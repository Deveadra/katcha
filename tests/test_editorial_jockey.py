"""Mocked Jockey upload, indexing and measured retrieval, with zero provider traffic."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from test_requirement_shot_matching import (  # noqa: F401
    match_context,
    prepare,
)
from test_shot_index_search import indexed_context, known_video  # noqa: F401

from katcha import db
from katcha.config import Settings
from katcha.editorial.jockey_schemas import JockeyAdmission, JockeySearchRequest
from katcha.editorial_models import EditorialRun
from katcha.services import editorial_jockey as jockey
from katcha.services.editorial_projects import EditorialConflict


class FakeProvider:
    def __init__(self):
        self.uploads = 0
        self.items = 0
        self.searches = 0
        self.assets = SimpleNamespace(create=self.upload, retrieve=self.asset_status)
        self.knowledge_store_items = SimpleNamespace(
            create=self.create_item, retrieve=self.item_status
        )
        self.knowledge_stores = SimpleNamespace(search=self.search)

    def upload(self, *, method, file):
        assert method == "direct" and file.read(10)
        self.uploads += 1
        return SimpleNamespace(id="asset-12345678", status="processing")

    def asset_status(self, *, asset_id):
        assert asset_id == "asset-12345678"
        return SimpleNamespace(status="ready")

    def create_item(self, *, knowledge_store_id, asset_id):
        assert knowledge_store_id == "store_12345678" and asset_id == "asset-12345678"
        self.items += 1
        return SimpleNamespace(id="item-12345678", status="processing")

    def item_status(self, *, knowledge_store_id, item_id):
        assert knowledge_store_id == "store_12345678" and item_id == "item-12345678"
        return SimpleNamespace(status="ready")

    def search(self, *, knowledge_store_id, query, search_options):
        assert knowledge_store_id == "store_12345678"
        assert query["text"] and search_options["video"]["modalities"]
        self.searches += 1
        return SimpleNamespace(
            data=[
                SimpleNamespace(
                    asset_type="video",
                    item_id="item-12345678",
                    rank=1,
                    matches=[SimpleNamespace(start_sec=0.2, end_sec=0.8)],
                ),
                SimpleNamespace(
                    asset_type="video",
                    item_id="unmapped-item",
                    rank=2,
                    matches=[SimpleNamespace(start_sec=0, end_sec=1)],
                ),
            ]
        )


@pytest.fixture
def configured(match_context, monkeypatch):
    context = match_context
    settings = Settings(
        _env_file=None,
        twelvelabs_jockey_api_token=SecretStr("fixture-secret"),
        twelvelabs_jockey_dispatch_enabled=True,
        twelvelabs_jockey_max_operations_per_project=3,
        twelvelabs_jockey_knowledge_stores={str(context[0]): "store_12345678"},
    )
    monkeypatch.setattr(jockey, "get_settings", lambda: settings)
    return context, FakeProvider()


def admit(context, **changes):
    return JockeyAdmission(
        expected_revision=1,
        external_inspection_authorized=True,
        provider_spend_approved=True,
    ).model_copy(update=changes)


def test_index_and_search_are_durable_scoped_and_review_only(configured):
    context, provider = configured
    channel, other, project, item, _, _, store = context
    body = admit(context)
    first = jockey.start_index(
        channel, project, item, body, actor="operator", client=provider, store=store
    )
    assert first["stage"] == "asset_processing"
    assert first["chargeable_operations_reserved"] == 1
    assert provider.uploads == 1
    replay = jockey.start_index(
        channel, project, item, body, actor="operator", client=provider, store=store
    )
    assert replay == first and provider.uploads == 1
    assert jockey.advance_index(channel, project, item, body, client=provider)["stage"] == (
        "asset_ready"
    )
    assert jockey.advance_index(channel, project, item, body, client=provider)["stage"] == (
        "item_processing"
    )
    ready = jockey.advance_index(channel, project, item, body, client=provider)
    assert ready["stage"] == "item_ready" and ready["status"] == "completed"
    assert ready["chargeable_operations_reserved"] == 2
    assert provider.items == 1
    assert jockey.advance_index(channel, project, item, body, client=provider) == ready
    assert provider.items == 1

    prep = prepare(context, "[VISUAL: red vehicle]")
    requirement = prep.artifacts["manifest"]["requirements"][0]["requirement_id"]
    request = JockeySearchRequest(
        preparation_run_id=prep.id,
        expected_revision=prep.input_revision,
        requirement_id=requirement,
        item_ids=(item,),
        idempotency_key="jockey-semantic",
        external_inspection_authorized=True,
        provider_spend_approved=True,
    )
    result = jockey.search_jockey(
        channel, project, request, actor="operator", client=provider, store=store
    )
    assert result["status"] == "completed" and provider.searches == 1
    suggestion = result["result"]["suggestions"][0]
    assert suggestion["measured_shot_id"] == "shot-0000"
    assert suggestion["measured_source_in_seconds"] == 0
    assert suggestion["measured_source_out_seconds"] == 1
    assert not suggestion["production_eligible"]
    assert not suggestion["verified_fact"]
    assert suggestion["human_review_required"]
    assert len(result["result"]["suggestions"]) == 1
    assert jockey.search_jockey(
        channel, project, request, actor="operator", client=provider, store=store
    ) == result
    assert provider.searches == 1
    with pytest.raises(EditorialConflict, match="budget"):
        jockey.search_jockey(
            channel, project, request.model_copy(update={"idempotency_key": "different",
                "requirement_id": requirement + "-other"}),
            actor="operator", client=provider, store=store,
        )
    with pytest.raises(EditorialConflict):
        jockey.get_index(other, project, item)
    with db.session_scope() as session:
        rows = list(session.scalars(select(EditorialRun).where(
            EditorialRun.options["target"].as_string().in_(
                [jockey.INDEX_TARGET, jockey.SEARCH_TARGET]
            )
        )))
    assert len(rows) == 2


def test_denied_admission_never_creates_remote_or_local_receipt(configured):
    context, provider = configured
    channel, _, project, item, _, _, store = context
    with pytest.raises(EditorialConflict, match="approval"):
        jockey.start_index(
            channel, project, item, admit(context, provider_spend_approved=False),
            actor="operator", client=provider, store=store,
        )
    assert provider.uploads == 0
    with db.session_scope() as session:
        assert not list(session.scalars(select(EditorialRun).where(
            EditorialRun.options["target"].as_string() == jockey.INDEX_TARGET
        )))


def test_uncertain_upload_blocks_all_automatic_retries(configured):
    context, provider = configured
    channel, _, project, item, _, _, store = context

    def broken_upload(*, method, file):
        provider.uploads += 1
        raise RuntimeError("fixture-secret should never appear in the response")

    provider.assets.create = broken_upload
    with pytest.raises(EditorialConflict, match="unknown") as error:
        jockey.start_index(
            channel, project, item, admit(context),
            actor="operator", client=provider, store=store,
        )
    assert "fixture-secret" not in str(error.value)
    receipt = jockey.start_index(
        channel, project, item, admit(context), actor="operator",
        client=provider, store=store,
    )
    assert receipt["status"] == "blocked" and not receipt["automatic_retry_allowed"]
    assert provider.uploads == 1


def test_provider_intervals_are_not_claimed_as_measured_facts():
    evidence = SimpleNamespace(
        timebase_numerator=1, timebase_denominator=1, duration_ticks=2,
        clip_sha256="a" * 64,
        shots=[
            SimpleNamespace(shot_id="s0", start_tick=0, end_tick=1,
                            keyframe_sha256="b" * 64),
            SimpleNamespace(shot_id="s1", start_tick=1, end_tick=2,
                            keyframe_sha256="c" * 64),
        ],
    )
    row = {
        "source": SimpleNamespace(clip_id="clip-1"),
        "item": SimpleNamespace(id="item-1", source_in_seconds=None,
                                source_out_seconds=None),
        "index": {"index_id": "index-1"},
        "evidence": evidence,
    }
    suggestion = {
        "clip_id": "clip-1", "source_in_seconds_unverified": 1.1,
        "source_out_seconds_unverified": 1.5, "verified_fact": False,
    }
    assert jockey._measured_candidates({"suggestions": [suggestion]}, [row], "req")[0][
        "measured_shot_id"
    ] == "s1"
    assert not jockey._measured_candidates(
        {"suggestions": [{**suggestion, "source_out_seconds_unverified": 9}]},
        [row], "req",
    )
