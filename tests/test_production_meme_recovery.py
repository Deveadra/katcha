"""Bounded acquisition receipts are synthetic; no provider or publishing proof."""

import asyncio
import json
import uuid
from datetime import timedelta
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import test_research_runtime as runtime
from PIL import Image
from test_production_meme_assets import Store
from test_source_original_production import make_clip

from katcha import db
from katcha.acquisition.adapters import DiscoveredCandidate, DiscoveryBatch
from katcha.orchestration import production_workflows
from katcha.production_models import ProductionAsset
from katcha.services import production_meme_assets as memes
from katcha.services.production_meme_plan import MemeBeat, MemePlan
from katcha.services.productions import register_short_production

research_db = runtime.research_db


def setup_draft(monkeypatch):
    channel, _ = runtime.channel()
    production = register_short_production(
        make_clip(channel, duration=158), channel_profile_id=channel, edit_blueprint_key="meme"
    )
    plan = MemePlan(
        beats=[
            MemeBeat(
                frame_index=i,
                visible_evidence="Synthetic visible frame",
                kind=kind,
                search_query=f"Specific reaction search {i}",
                caption=f"Caption {i}",
                duration_seconds=4,
            )
            for i, kind in enumerate(["image", "audio", "image", "video"])
        ]
    )
    evidence = {"frame_times": [4, 25, 46, 66], "plan": plan.model_dump(mode="json")}
    monkeypatch.setattr(memes, "plan_memes", lambda *args: (plan, evidence))
    monkeypatch.setattr(memes, "ObjectStore", Store)
    monkeypatch.setattr(memes, "assert_mutation_authority", lambda *args: None)
    return production


def receipt(production):
    with db.session_scope() as session:
        row = session.get(ProductionAsset, uuid.uuid5(production.id, "meme-search:1"))
        return row.asset_metadata


def test_failed_and_short_candidates_research_once_with_durable_evidence(research_db, monkeypatch):
    production = setup_draft(monkeypatch)
    searches, downloads = [], []

    def discover(query, cursor):
        # Prove the started receipt exists before the external provider accepts work.
        assert receipt(production)["search_attempts"][-1]["status"] == "started"
        index = int(query["reservation_key"].split(":")[3])
        alternate = query["reservation_key"].endswith(":alternative")
        searches.append(query)
        prefix = "good" if alternate or index > 1 else "bad"
        assert "last at least 4.0 seconds" in query["operator_request"]
        assert "avoid unkeyed green-screen" in query["operator_request"]
        assert "Synthetic visible frame" in query["operator_request"]
        assert f"Caption {index}" in query["operator_request"]
        if alternate:
            assert "requested media kind" in query["operator_request"]
            assert "at least 4.0 seconds" in query["operator_request"]
        return DiscoveryBatch(
            items=(
                DiscoveredCandidate(
                    source_url=f"https://example.com/{prefix}/{index}?token=private-secret",
                    title="Synthetic reaction",
                ),
            )
        )

    def download(url, medium, seconds):
        attempt = receipt(production)["search_attempts"][-1]
        assert attempt["candidates"][0]["status"] == "downloading"
        downloads.append(url)
        if "/bad/0" in url:
            response = httpx.Response(403, request=httpx.Request("GET", url))
            raise httpx.HTTPStatusError(
                "private-secret", request=response.request, response=response
            )
        actual = 1.25 if "/bad/1" in url else seconds
        return url.encode(), f"{medium}/fixture", actual, url

    monkeypatch.setattr(
        memes, "WebScoutDiscoveryAdapter", lambda: SimpleNamespace(discover=discover)
    )
    monkeypatch.setattr(memes, "download_meme", download)
    assert memes.prepare_production_memes(str(production.id))["assets"] == 4
    saved = receipt(production)
    assert len(searches) == len(downloads) == 6
    assert saved["status"] == "completed"
    assert "private-secret" not in json.dumps(saved)
    assert saved["search_attempts"][0]["candidates"][0]["failure"] == {
        "code": "http_status",
        "http_status": 403,
    }
    short = saved["search_attempts"][2]["candidates"][0]
    assert short["failure"]["code"] == "short_media"
    assert short["actual_duration_seconds"] == 1.25
    assert short["required_duration_seconds"] == 4
    # Completed work is reused; failed requests are never blindly dispatched again.
    with db.session_scope() as session:
        events = (
            session.query(ProductionAsset)
            .filter(
                ProductionAsset.production_id == production.id,
                ProductionAsset.kind.like("meme_asset_%"),
            )
            .all()
        )
        assert len(events) == 4
        assert all(row.asset_metadata["event"]["duration_seconds"] == 4 for row in events)
        assert all("private-secret" not in json.dumps(row.asset_metadata) for row in events)


def test_exhaustion_is_bounded_and_retains_failed_candidate_evidence(research_db, monkeypatch):
    production = setup_draft(monkeypatch)
    searches, downloads = [], []

    def discover(query, cursor):
        searches.append(query)
        return DiscoveryBatch(
            items=tuple(
                DiscoveredCandidate(
                    source_url=f"https://example.com/{len(searches)}/{index}", title="Fixture"
                )
                for index in range(5)
            )
        )

    def download(url, *args):
        downloads.append(url)
        raise ValueError("Never persist arbitrary exception text with secret=exposed")

    monkeypatch.setattr(
        memes, "WebScoutDiscoveryAdapter", lambda: SimpleNamespace(discover=discover)
    )
    monkeypatch.setattr(memes, "download_meme", download)
    with pytest.raises(RuntimeError, match="no usable"):
        memes.prepare_production_memes(str(production.id))
    assert len(searches) == 8 and len(downloads) == 24
    saved = receipt(production)
    assert saved["status"] == "failed"
    assert all(attempt["status"] == "exhausted" for attempt in saved["search_attempts"])
    assert "exposed" not in json.dumps(saved)
    with pytest.raises(RuntimeError, match="previously failed"):
        memes.prepare_production_memes(str(production.id))
    assert len(searches) == 8


def test_uncertain_search_remains_durable_and_nonreplayable(research_db, monkeypatch):
    production = setup_draft(monkeypatch)
    calls = []

    def discover(query, cursor):
        calls.append(query)
        raise RuntimeError("Provider result is uncertain")

    monkeypatch.setattr(
        memes, "WebScoutDiscoveryAdapter", lambda: SimpleNamespace(discover=discover)
    )
    with pytest.raises(RuntimeError, match="uncertain"):
        memes.prepare_production_memes(str(production.id))
    saved = receipt(production)
    assert saved["status"] == "failed"
    assert saved["planning"]["plan"]["beats"][0]["frame_index"] == 0
    assert saved["search_attempts"][0]["status"] == "started"
    with pytest.raises(RuntimeError, match="previously failed"):
        memes.prepare_production_memes(str(production.id))
    assert len(calls) == 1


def test_static_lazy_images_and_gif_video_are_resolved_without_scripts(monkeypatch):
    pages = {
        "https://example.com/image": (
            b'<img src="/placeholder.png" data-src="/reaction.jpg">',
            "text/html",
        ),
        "https://example.com/video": (b'<img data-original="/reaction.gif">', "text/html"),
        "https://example.com/reaction.jpg": (b"actual image", "image/jpeg"),
        "https://example.com/reaction.gif": (b"actual gif", "image/gif"),
    }
    monkeypatch.setattr(memes, "_fetch", lambda url: (*pages[url], url))
    monkeypatch.setattr(
        memes, "_normalize", lambda data, mime, kind, seconds, url: (data, mime, seconds, url)
    )
    assert memes.download_meme("https://example.com/image", "image", 4)[0] == b"actual image"
    assert memes.download_meme("https://example.com/video", "video", 4)[0] == b"actual gif"


def test_animated_reaction_gif_can_be_used_as_a_sanitized_still():
    first = Image.new("RGB", (8, 8), (220, 10, 10))
    second = Image.new("RGB", (8, 8), (10, 10, 220))
    source = BytesIO()
    first.save(source, format="GIF", save_all=True, append_images=[second], duration=80, loop=0)

    encoded, mime, seconds, url = memes._normalize(
        source.getvalue(), "image/gif", "image", 4.6, "https://media.example/reaction.gif"
    )

    assert mime == "image/png"
    assert seconds == 4.6
    assert url == "https://media.example/reaction.gif"
    with Image.open(BytesIO(encoded)) as result:
        assert result.format == "PNG"
        assert result.n_frames == 1
        assert result.convert("RGB").getpixel((0, 0)) == (220, 10, 10)


def test_short_animated_reaction_gif_loops_to_its_planned_video_duration(monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if args[0] == "ffprobe":
            probe = {
                "streams": [{"codec_type": "video"}],
                "format": {"duration": "1.5", "format_name": "gif"},
            }
            return SimpleNamespace(stdout=json.dumps(probe).encode())
        Path(args[-1]).write_bytes(b"normalized reaction video")
        return SimpleNamespace(stdout=b"")

    monkeypatch.setattr(memes.subprocess, "run", run)
    data, mime, seconds, _ = memes._normalize(
        b"synthetic animated gif", "image/gif", "video", 4.6, "https://media.example/reaction.gif"
    )

    assert data == b"normalized reaction video"
    assert mime == "video/mp4"
    assert seconds == 4.6
    assert "-stream_loop" in calls[1]
    assert calls[1][calls[1].index("-stream_loop") + 1] == "-1"
    assert calls[1][calls[1].index("-t") + 1] == "4.6"


def test_deadline_does_not_start_more_downloads(monkeypatch):
    token = memes._PREPARATION_DEADLINE.set(10)
    monkeypatch.setattr(memes.time, "monotonic", lambda: 11)
    try:
        with pytest.raises(TimeoutError, match="bounded"):
            memes._operation_timeout(20)
    finally:
        memes._PREPARATION_DEADLINE.reset(token)


@pytest.mark.parametrize("new_history,expected", [(False, 12), (True, 25)])
def test_workflow_keeps_replayed_timeout_and_paid_once(monkeypatch, new_history, expected):
    calls = []

    async def activity(name, *args, **kwargs):
        calls.append((name, kwargs))
        return {}

    monkeypatch.setattr(production_workflows, "execute_resource_activity", activity)
    monkeypatch.setattr(
        production_workflows.workflow,
        "patched",
        lambda marker: marker == "production-meme-assets-v1" or new_history,
    )
    asyncio.run(production_workflows.ShortProductionWorkflow().run("fixture", "render"))
    kwargs = next(kwargs for name, kwargs in calls if name == "prepare_meme_assets")
    assert kwargs["start_to_close_timeout"] == timedelta(minutes=expected)
    assert kwargs["retry_policy"].maximum_attempts == 1
