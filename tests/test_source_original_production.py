"""Offline operator acquisition and source-only production regression coverage."""

import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
import test_research_runtime as runtime

from katcha import db
from katcha.acquisition_models import DiscoveryCandidate
from katcha.domain import SourceUsageMode
from katcha.models import Clip, ClipFeature, SourceItem
from katcha.orchestration import production_activities
from katcha.production_models import Production, ProductionScript
from katcha.services.acquisition import clip_acquisition_state, promote_discovery_candidate
from katcha.services.command_center import infer_edit_blueprint_key
from katcha.services.ingestion_sources import upsert_ingestion_source
from katcha.services.productions import register_regeneration, register_short_production
from katcha.services.publications import _approved_render_key

research_db = runtime.research_db


@pytest.mark.parametrize("usage", ["discovery_only", "blocked", "candidate_review"])
def test_download_is_not_vetoed_by_source_preference_or_assessment(research_db, usage):
    channel_id, _ = runtime.channel()
    source = upsert_ingestion_source(
        source_key=str(uuid.uuid4()),
        name="Source",
        adapter_key="rss_atom",
        adapter_version="v1",
        platform="youtube",
        usage_mode=SourceUsageMode(usage),
    )
    with db.session_scope() as session:
        candidate = DiscoveryCandidate(
            adapter_key="youtube",
            source_url="https://youtube.com/watch?v=test",
            canonical_url="https://youtube.com/watch?v=test",
            platform="youtube",
            candidate_metadata={
                "ingestion_source_id": str(source.id),
                "channel_profile_id": str(channel_id),
            },
        )
        session.add(candidate)
        session.flush()
        candidate_id = candidate.id
    promoted = promote_discovery_candidate(candidate_id)
    assert promoted.source_metadata["rights_basis"] == "unknown"
    assert promoted.source_metadata["rights_assessment_id"] is None
    assert promote_discovery_candidate(candidate_id).id == promoted.id


def make_clip(channel_id, *, duration=12):
    with db.session_scope() as session:
        clip = Clip(
            sha256=uuid.uuid4().hex * 2,
            storage_key="raw/fixture.mp4",
            duration_seconds=Decimal(duration),
            extension="mp4",
        )
        session.add(clip)
        session.flush()
        source = SourceItem(
            source_url="https://example.com/video",
            canonical_url="https://example.com/video",
            platform="web",
            clip_id=clip.id,
            source_metadata={"channel_profile_id": str(channel_id)},
        )
        session.add(source)
        session.flush()
        session.add(
            DiscoveryCandidate(
                adapter_key="web",
                source_url=source.source_url,
                canonical_url=source.canonical_url,
                platform="web",
                source_item_id=source.id,
                candidate_metadata={"channel_profile_id": str(channel_id)},
            )
        )
        return clip.id


def test_unscored_download_prepares_without_ai_script_or_tts(research_db, monkeypatch):
    channel_id, _ = runtime.channel()
    clip_id = make_clip(channel_id)
    production = register_short_production(
        clip_id,
        channel_profile_id=channel_id,
        edit_blueprint_key="source_original",
        idempotency_key="plain-clip",
    )
    assert production.analysis_snapshot["candidate_score"] is None
    assert production.analysis_snapshot["acquisition"]["eligible"] is False
    assert not clip_acquisition_state(clip_id).eligible
    monkeypatch.setattr(
        production_activities,
        "generate_short_scripts",
        lambda *a, **k: pytest.fail("Source-only must not call scripting AI"),
    )
    monkeypatch.setattr(
        production_activities, "ObjectStore", lambda: SimpleNamespace(ensure_bucket=lambda: None)
    )
    result = production_activities.generate_script_candidates(str(production.id))
    assert result["skipped"] and result["candidate_count"] == 1
    assert production_activities.select_script_candidate(str(production.id))["reused"]
    assert production_activities.generate_narration_assets(str(production.id))["skipped"]
    assert production_activities.generate_script_candidates(str(production.id))["reused"]
    with db.session_scope() as session:
        saved = session.get(Production, production.id)
        script = session.get(ProductionScript, saved.selected_script_id)
        assert script.narration == "" and script.script_metadata["segments"] == []
        assert session.get(ClipFeature, clip_id) is None
        saved.status = "approved"
    with db.session_scope() as session, pytest.raises(ValueError, match="production-eligible"):
        _approved_render_key(session, source_kind="production", source_id=production.id)


def test_source_original_still_needs_actual_media_measurements(research_db):
    channel_id, _ = runtime.channel()
    with pytest.raises(ValueError, match="measured duration"):
        register_short_production(
            make_clip(channel_id, duration=0),
            channel_profile_id=channel_id,
            edit_blueprint_key="source_original",
        )


@pytest.mark.parametrize(
    ("prompt", "key"),
    [
        ("Prepare this trailer", "source_original"),
        ("Prepare this without commentary", "source_original"),
        ("Add commentary", "persona_commentary"),
        ("Make a meme edit", "meme"),
        ("Prepare without memes", "source_original"),
    ],
)
def test_request_blueprint_defaults(prompt, key):
    assert infer_edit_blueprint_key(prompt) == key


@pytest.mark.parametrize("blueprint", ["source_original", "meme"])
def test_unqualified_source_only_draft_can_regenerate_after_failure(research_db, blueprint):
    channel_id, _ = runtime.channel()
    clip_id = make_clip(channel_id)
    parent = register_short_production(
        clip_id, channel_profile_id=channel_id, edit_blueprint_key=blueprint
    )
    with db.session_scope() as session:
        session.get(Production, parent.id).status = "failed"
    child = register_regeneration(parent.id, stage="script", idempotency_key="retry")
    assert child.parent_production_id == parent.id
    assert child.generation == parent.generation + 1
    assert child.edit_blueprint_snapshot == parent.edit_blueprint_snapshot
    assert child.analysis_snapshot["acquisition"]["eligible"] is False
    assert child.analysis_snapshot["source_only_preparation"] is True
    assert register_regeneration(parent.id, stage="script", idempotency_key="retry").id == child.id
