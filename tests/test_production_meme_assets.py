"""Synthetic discovery-to-manifest evidence; no live provider or publishing claim."""

import io
import json
import uuid
from types import SimpleNamespace

import pytest
import test_research_runtime as runtime
from PIL import Image
from test_source_original_production import make_clip

from katcha import db
from katcha.acquisition.adapters import DiscoveredCandidate, DiscoveryBatch
from katcha.editing.blueprints import meme_v1, source_original_v1
from katcha.production_models import Production, ProductionAsset
from katcha.rendering.blueprint_manifest import build_blueprint_render_manifest
from katcha.rendering.manifest import channel_01_brand_v1
from katcha.services import production_meme_assets as memes
from katcha.services.productions import register_short_production

research_db = runtime.research_db


class Store:
    def __init__(self):
        self.objects = {}

    def put_bytes(self, data, key, content_type=None):
        self.objects[key] = data

    def exists(self, key):
        return key in self.objects


def test_search_download_receipt_manifest_and_retry_reuse(research_db, monkeypatch):
    channel, _ = runtime.channel()
    production = register_short_production(
        make_clip(channel), channel_profile_id=channel, edit_blueprint_key="meme"
    )
    store = Store()
    calls = []

    def discover(query, cursor):
        calls.append(query)
        return DiscoveryBatch(
            items=(
                DiscoveredCandidate(
                    source_url="https://example.com/reaction", title="Relevant reaction"
                ),
            )
        )

    monkeypatch.setattr(
        memes, "WebScoutDiscoveryAdapter", lambda: SimpleNamespace(discover=discover)
    )
    monkeypatch.setattr(memes, "ObjectStore", lambda: store)
    monkeypatch.setattr(memes, "assert_mutation_authority", lambda *a: None)
    monkeypatch.setattr(
        memes,
        "download_meme",
        lambda url, medium, seconds: (
            medium.encode(),
            {"image": "image/png", "audio": "audio/wav", "video": "video/mp4"}[medium],
            seconds,
            url,
        ),
    )
    result = memes.prepare_production_memes(str(production.id))
    assert result["assets"] == 3 and len(calls) == 3
    assert {query["channel_profile_id"] for query in calls} == {str(channel)}
    with db.session_scope() as session:
        saved = session.get(Production, production.id)
        events = memes.meme_events_for_production(session, saved, store)
        manifest = build_blueprint_render_manifest(
            render_id=str(production.id),
            channel_profile_id=str(channel),
            brand=channel_01_brand_v1(),
            blueprint=meme_v1(),
            source_storage_key="raw/fixture.mp4",
            source_duration_seconds=12,
            output_key="out.mp4",
            meme_events=events,
        )
        assert {event.kind for event in manifest.meme_events} == {"image", "audio", "video"}
        receipt = session.get(ProductionAsset, uuid.uuid5(production.id, "meme-search:1"))
        assert receipt.asset_metadata["rights_status"] == "not_assessed"
    assert memes.prepare_production_memes(str(production.id))["reused"]
    assert len(calls) == 3
    del store.objects[events[0].storage_key]
    with pytest.raises(RuntimeError, match="missing"):
        memes.prepare_production_memes(str(production.id))


def test_empty_meme_search_is_explicit_failure_and_not_blindly_retried(research_db, monkeypatch):
    channel, _ = runtime.channel()
    production = register_short_production(
        make_clip(channel), channel_profile_id=channel, edit_blueprint_key="meme"
    )
    monkeypatch.setattr(memes, "ObjectStore", Store)
    monkeypatch.setattr(memes, "assert_mutation_authority", lambda *a: None)
    monkeypatch.setattr(
        memes,
        "WebScoutDiscoveryAdapter",
        lambda: SimpleNamespace(discover=lambda *a: DiscoveryBatch(items=())),
    )
    with pytest.raises(RuntimeError, match="no usable"):
        memes.prepare_production_memes(str(production.id))
    with pytest.raises(RuntimeError, match="previously failed"):
        memes.prepare_production_memes(str(production.id))


def test_meme_image_download_normalizes_actual_pixels(monkeypatch):
    raw = io.BytesIO()
    Image.new("RGB", (32, 32), "red").save(raw, format="JPEG")
    monkeypatch.setattr(memes, "_fetch", lambda url: (raw.getvalue(), "image/jpeg", url))
    data, mime, seconds, _ = memes.download_meme("https://example.com/a.jpg", "image", 2)
    assert mime == "image/png" and seconds == 2
    assert Image.open(io.BytesIO(data)).size == (32, 32)


def test_meme_html_resolves_only_downloaded_media(monkeypatch):
    raw = io.BytesIO()
    Image.new("RGB", (16, 16), "blue").save(raw, format="PNG")
    monkeypatch.setattr(
        memes,
        "_fetch",
        lambda url: (
            (b'<meta property="og:image" content="/reaction.png">', "text/html", url)
            if url.endswith("page")
            else (raw.getvalue(), "image/png", url)
        ),
    )
    _, _, _, url = memes.download_meme("https://example.com/page", "image", 1)
    assert url == "https://example.com/reaction.png"


def test_meme_manifest_requires_actual_assets_and_rejects_cross_production():
    kwargs = dict(
        render_id="production",
        channel_profile_id="channel",
        brand=channel_01_brand_v1(),
        blueprint=meme_v1(),
        source_storage_key="raw/a.mp4",
        source_duration_seconds=12,
        output_key="out.mp4",
    )
    with pytest.raises(ValueError, match="requires prepared"):
        build_blueprint_render_manifest(**kwargs)
    with pytest.raises(ValueError, match="belong to this production"):
        build_blueprint_render_manifest(
            **kwargs,
            meme_events=[
                dict(
                    id="1",
                    kind="image",
                    storage_key="productions/another/meme/x.png",
                    source_url="https://example.com/x.png",
                    title="Reaction",
                    start_seconds=1,
                    duration_seconds=1,
                )
            ],
        )
    kwargs["blueprint"] = source_original_v1()
    assert not build_blueprint_render_manifest(**kwargs).meme_events


@pytest.mark.parametrize("plain_text", [False, True])
def test_gemini_search_uses_grounded_sources_and_records_actual_usage(monkeypatch, plain_text):
    from google import genai

    from katcha.acquisition import gemini_web_scout as scout

    calls = []
    response = SimpleNamespace(
        text=(
            "Observed grounded result"
            if plain_text
            else json.dumps({"items": [{"source_url": "https://invented.invalid/a"}]})
        ),
        usage_metadata=SimpleNamespace(
            prompt_token_count=100, candidates_token_count=40, thoughts_token_count=10
        ),
        candidates=[
            SimpleNamespace(
                grounding_metadata=SimpleNamespace(
                    grounding_chunks=[
                        SimpleNamespace(
                            web=SimpleNamespace(
                                uri="https://example.com/observed", title="Observed"
                            )
                        )
                    ]
                )
            )
        ],
    )
    monkeypatch.setattr(
        genai,
        "Client",
        lambda **k: SimpleNamespace(
            models=SimpleNamespace(generate_content=lambda **k: response), close=lambda: None
        ),
    )
    monkeypatch.setattr(scout, "assert_ai_budget", lambda *a: None)
    monkeypatch.setattr(scout, "assert_mutation_authority", lambda *a, **k: None)
    monkeypatch.setattr(scout, "record_usage", lambda **k: calls.append(k))
    result = scout.discover_gemini(
        {"operator_request": "reaction image"}, {}, SimpleNamespace(gemini_api_key="fixture"), 3
    )
    assert [item.source_url for item in result.items] == ["https://example.com/observed"]
    assert result.items[0].provenance_claims["discovered_via"] == "gemini_google_search"
    assert calls[0]["output_units"] == 50
    assert calls[0]["metadata"]["search_cost_usd"] == "0.014"


def test_gemini_uncertain_search_is_not_repeated_through_paid_provider(monkeypatch):
    import httpx

    from katcha.acquisition import gemini_web_scout, web_scout
    from katcha.acquisition.adapters import DiscoveryProviderError

    settings = SimpleNamespace(
        ai_enabled=True,
        resolved_ai_execution_mode=lambda: "live",
        codex_enabled=False,
        chatgpt_host_id=None,
        gemini_api_key="fixture",
        openai_api_key="fixture",
        allow_paid_openai_fallback=True,
    )
    monkeypatch.setattr(web_scout, "get_settings", lambda: settings)

    def uncertain(*args):
        raise httpx.ReadTimeout("uncertain accepted request")

    monkeypatch.setattr(gemini_web_scout, "discover_gemini", uncertain)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("Must not replay search"))
    with pytest.raises(DiscoveryProviderError) as failure:
        web_scout.WebScoutDiscoveryAdapter().discover({"operator_request": "reaction"}, {})
    assert failure.value.kind == "provider_result_unknown"


@pytest.mark.parametrize(
    "declaration",
    [
        '<a href="/media/fail.mp3?download=1">Download sound</a>',
        "<button onclick=\"play('/media/fail.mp3', this)\">Play</button>",
        '<script type="application/ld+json">'
        '{"@graph":[{"@type":"AudioObject","contentUrl":"/media/fail.mp3"}]}'
        "</script>",
        '<source src="/media/fail.mp3" type="audio/mpeg">',
    ],
)
def test_sound_page_static_downloads_take_priority_over_artwork(monkeypatch, declaration):
    html = '<meta property="og:image" content="/artwork.png">' * 12 + declaration
    visited = []

    def fetch(url):
        visited.append(url)
        if url.endswith("page"):
            return html.encode(), "text/html", url
        return b"actual downloaded audio", "audio/mpeg", url

    monkeypatch.setattr(memes, "_fetch", fetch)
    monkeypatch.setattr(
        memes, "_normalize", lambda data, mime, medium, seconds, url: (data, mime, seconds, url)
    )
    result = memes.download_meme("https://example.com/page", "audio", 2)
    assert result[0] == b"actual downloaded audio"
    assert visited == ["https://example.com/page", result[3]]
    assert result[3].startswith("https://example.com/media/fail.mp3")


def test_static_parser_does_not_execute_or_interpret_expressions():
    parser = memes._MediaLinks()
    parser.feed("""
        <button onclick="play('/sound.mp3' + token)">No expression</button>
        <button onclick="evil('/sound.mp3')">No other function</button>
        <script>fetch('/sound.mp3')</script>
        <script type="application/ld+json">not JSON</script>
        <a href="/login">Not media</a>
    """)
    assert parser.candidates("audio") == []


def test_candidate_limit_deduplicates_and_filters_before_bounding():
    parser = memes._MediaLinks()
    parser.feed('<meta property="og:image" content="/art.png">' * 20)
    parser.feed('<a href="/sound.mp3">sound</a>' * 20)
    for index in range(12):
        parser.feed(f'<a href="/sound-{index}.mp3">sound</a>')
    assert parser.candidates("audio") == ["/sound.mp3", *[f"/sound-{i}.mp3" for i in range(7)]]


def test_public_transport_and_identified_client_are_used_for_each_redirect(monkeypatch):
    import httpx

    permissions = []

    def transport(permission):
        permissions.append(permission)

        def respond(request):
            assert request.headers["user-agent"] == "KatchaMediaIntake/1.0 (+https://katcha.stream)"
            if request.url.host == "example.com":
                return httpx.Response(302, headers={"location": "https://cdn.example.com/a.mp3"})
            return httpx.Response(200, content=b"sound", headers={"content-type": "audio/mpeg"})

        return httpx.MockTransport(respond)

    monkeypatch.setattr(memes, "PublicDestinationTransport", transport)
    assert memes._fetch("https://example.com/page") == (
        b"sound",
        "audio/mpeg",
        "https://cdn.example.com/a.mp3",
    )
    assert [permission.host for permission in permissions] == ["example.com", "cdn.example.com"]
    assert all(not permission.allows_host("127.0.0.1") for permission in permissions)


def test_access_denied_is_not_retried_or_bypassed(monkeypatch):
    import httpx

    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(403)

    monkeypatch.setattr(
        memes, "PublicDestinationTransport", lambda permission: httpx.MockTransport(respond)
    )
    with pytest.raises(httpx.HTTPStatusError):
        memes._fetch("https://example.com/blocked")
    assert len(requests) == 1
