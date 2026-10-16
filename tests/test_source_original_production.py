"""Offline operator acquisition and source-only production regression coverage."""

import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
import test_research_runtime as runtime
from sqlalchemy import select

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


@pytest.mark.parametrize("blueprint", ["source_original", "meme"])
@pytest.mark.parametrize("stage", ["voice", "render"])
def test_source_only_regeneration_reuses_one_local_selected_script(
    research_db, blueprint, stage,
):
    channel_id, _ = runtime.channel()
    parent = register_short_production(
        make_clip(channel_id), channel_profile_id=channel_id, edit_blueprint_key=blueprint,
    )
    production_activities.generate_script_candidates(str(parent.id))
    with db.session_scope() as session:
        saved = session.get(Production, parent.id)
        saved.status = "failed"
        parent_script_id = saved.selected_script_id
    child = register_regeneration(parent.id, stage=stage, idempotency_key="retry-selected")
    assert child.status == ("voiced" if stage == "render" else "scripted")
    assert child.edit_blueprint_snapshot == parent.edit_blueprint_snapshot
    with db.session_scope() as session:
        scripts = list(session.scalars(
            select(ProductionScript).where(ProductionScript.production_id == child.id)
        ))
        assert len(scripts) == 1
        assert scripts[0].selected and scripts[0].id == child.selected_script_id
        assert scripts[0].id != parent_script_id
        assert scripts[0].narration == "" and scripts[0].provider == "local"
        assert scripts[0].script_metadata["segments"] == []
    assert register_regeneration(
        parent.id, stage=stage, idempotency_key="retry-selected",
    ).id == child.id


@pytest.mark.parametrize("blueprint", ["source_original", "meme"])
@pytest.mark.parametrize("stage", ["voice", "render"])
@pytest.mark.parametrize("broken", ["missing_script", "extra_script", "unselected", "missing_id"])
def test_source_only_regeneration_rejects_incomplete_or_unselected_scripts(
    research_db, blueprint, stage, broken,
):
    channel_id, _ = runtime.channel()
    parent = register_short_production(
        make_clip(channel_id), channel_profile_id=channel_id, edit_blueprint_key=blueprint,
    )
    production_activities.generate_script_candidates(str(parent.id))
    with db.session_scope() as session:
        saved = session.get(Production, parent.id)
        saved.status = "failed"
        script = session.get(ProductionScript, saved.selected_script_id)
        if broken == "missing_script":
            session.delete(script)
        elif broken == "extra_script":
            session.add(ProductionScript(
                production_id=parent.id, candidate_index=1, style="unexpected",
                narration="", provider="local", model="fixture", prompt_version="fixture",
            ))
        elif broken == "unselected":
            script.selected = False
        else:
            saved.selected_script_id = None
    with pytest.raises(ValueError, match="complete script set|selected script"):
        register_regeneration(parent.id, stage=stage)
    with db.session_scope() as session:
        assert session.get(Production, parent.id).status == "failed"
        assert not list(session.scalars(
            select(Production).where(Production.parent_production_id == parent.id)
        ))


@pytest.mark.parametrize("script_count", [1, 2, 3])
def test_commentary_regeneration_still_requires_three_scripts(
    research_db, monkeypatch, script_count,
):
    channel_id, _ = runtime.channel()
    clip_id = make_clip(channel_id)
    parent = register_short_production(
        clip_id, channel_profile_id=channel_id, edit_blueprint_key="source_original",
    )
    production_activities.generate_script_candidates(str(parent.id))
    with db.session_scope() as session:
        saved = session.get(Production, parent.id)
        saved.status = "failed"
        snapshot = dict(saved.edit_blueprint_snapshot)
        snapshot["narration"] = {**snapshot["narration"], "mode": "persona_voice"}
        saved.edit_blueprint_snapshot = snapshot
        for index in range(1, script_count):
            session.add(ProductionScript(
                production_id=parent.id, candidate_index=index, style="fixture-commentary",
                narration="Fixture commentary", provider="fixture", model="fixture",
                prompt_version="fixture",
            ))
    # Isolate the script-set contract from unrelated publication eligibility.
    monkeypatch.setattr(
        "katcha.services.productions.assert_clip_production_eligible", clip_acquisition_state,
    )
    if script_count != 3:
        with pytest.raises(ValueError, match="complete script set"):
            register_regeneration(parent.id, stage="voice")
    else:
        child = register_regeneration(parent.id, stage="voice")
        with db.session_scope() as session:
            scripts = list(session.scalars(
                select(ProductionScript).where(ProductionScript.production_id == child.id)
            ))
            assert len(scripts) == 3
            assert sum(script.selected for script in scripts) == 1
            assert child.selected_script_id in [script.id for script in scripts]
