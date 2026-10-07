"""Measured narration contracts using generated PCM, never live TTS or publishing."""

import io
import uuid
import wave
from unittest.mock import Mock

import pytest
from test_editorial_assets import completed_scout
from test_editorial_projects import saved as _saved

from katcha import db
from katcha.editorial import narration
from katcha.editorial.project_schemas import EditorialDraft
from katcha.editorial.run_schemas import StartEditorialRun
from katcha.editorial.visual_compiler import compile_project_visuals
from katcha.editorial.visual_schemas import EditorialRenderManifest, StoryboardPlan
from katcha.editorial_models import EditorialNarration, EditorialProject
from katcha.provider_setting_models import ChannelProviderSetting
from katcha.services.editorial_projects import EditorialConflict
from katcha.services.editorial_runs import checkpoint, start_run

saved = _saved


def wav(frames=44101, rate=22050):
    data = io.BytesIO()
    with wave.open(data, "wb") as file:
        file.setnchannels(1)
        file.setsampwidth(2)
        file.setframerate(rate)
        file.writeframes(b"\0\0" * frames)
    return data.getvalue()


@pytest.fixture
def project(saved, monkeypatch):
    scout = completed_scout(saved)
    store = Mock()
    monkeypatch.setattr(narration, "ObjectStore", lambda: store)
    return scout, store


def upload(project, **changes):
    scout, _ = project
    values = dict(
        revision=1,
        beat_id="beat-1",
        idempotency_key="recording-1",
        audio=wav(),
        actor="operator",
        permitted_use=True,
    )
    values.update(changes)
    return narration.import_narration(scout.channel_profile_id, scout.project_id, **values)


def storyboard(recording):
    return StoryboardPlan.model_validate(
        {
            "presentation_mode": "narrated",
            "narration_ids": {"beat-1": recording["id"]},
            "beats": [{"beat_id": "beat-1", "layout": "quote", "quote_source_id": "source-1"}],
        }
    )


def assets(project):
    scout, _ = project
    row = start_run(
        scout.channel_profile_id,
        scout.project_id,
        StartEditorialRun(
            expected_revision=1,
            idempotency_key="quote-assets",
            target="acquire_assets",
            scout_run_id=scout.id,
            asset_candidate_ids=["asset"],
        ),
        actor="operator",
    )
    checkpoint(str(row.id), 1, status="completed")
    return row


def compile_recording(project, recording, asset_run):
    scout, _ = project
    return compile_project_visuals(
        scout.channel_profile_id, scout.project_id, 1, asset_run.id, storyboard(recording)
    )


def test_upload_replay_is_exact_and_retains_revocation(project):
    first = upload(project)
    assert first["duration_seconds"] == 44101 / 22050
    assert first["transcript_verified"] is False
    assert upload(project) == first
    project[1].put_bytes.assert_called_once()
    with pytest.raises(EditorialConflict, match="identity"):
        upload(project, audio=wav(frames=100))
    scout, _ = project
    narration.revoke_narration(
        scout.channel_profile_id, scout.project_id, uuid.UUID(first["id"]), actor="operator"
    )
    assert upload(project)["status"] == "revoked"


@pytest.mark.parametrize("data", [b"", b"not audio", wav()[:-8], wav(0), wav(rate=4000)])
def test_bad_audio_is_rejected_before_storage(project, data):
    with pytest.raises(ValueError):
        upload(project, audio=data)
    project[1].put_bytes.assert_not_called()


def test_edit_during_upload_cannot_attach_stale_audio(project):
    scout, store = project

    def edit(*args, **kwargs):
        with db.session_scope() as session:
            session.get(EditorialProject, scout.project_id).revision = 2

    store.put_bytes.side_effect = edit
    with pytest.raises(EditorialConflict, match="Script changed"):
        upload(project)
    with db.session_scope() as session:
        assert not session.query(EditorialNarration).count()


def test_storage_failure_retries_same_immutable_object(project):
    project[1].put_bytes.side_effect = [TimeoutError(), None]
    with pytest.raises(TimeoutError):
        upload(project)
    uploaded = upload(project)
    assert uploaded["status"] == "active"
    calls = project[1].put_bytes.call_args_list
    assert calls[0] == calls[1]


def test_voice_off_blocks_upload_and_existing_narrated_preview(project):
    recording = upload(project)
    asset_run = assets(project)
    scout, store = project
    with db.session_scope() as session:
        session.add(
            ChannelProviderSetting(
                channel_profile_id=scout.channel_profile_id,
                provider="elevenlabs",
                enabled=False,
                config={},
            )
        )
    with pytest.raises(EditorialConflict, match="Voice is off"):
        upload(project, idempotency_key="new")
    with pytest.raises(EditorialConflict, match="Voice is off"):
        compile_recording(project, recording, asset_run)
    assert store.put_bytes.call_count == 1


def test_compilation_uses_measured_samples_and_revocation_invalidates_review(
    project, saved, monkeypatch
):
    from katcha.editorial.render import render_project
    from katcha.editorial.review_schemas import ReviewEditorialRender
    from katcha.rendering.client import RenderResult
    from katcha.services.editorial_reviews import review_render, review_status

    recording = upload(project)
    asset_run = assets(project)
    result = compile_recording(project, recording, asset_run)
    assert result.version == "editorial-render-v6"
    assert result.brand is not None
    assert result.timeline[0].duration_frames == 61
    assert sum(c.duration_frames for c in result.timeline[0].captions) == 61
    assert result.output_duration_seconds == 61 / 30
    assert EditorialRenderManifest.model_validate_json(result.model_dump_json()) == result
    scout, _ = project
    row = start_run(
        scout.channel_profile_id,
        scout.project_id,
        StartEditorialRun(
            expected_revision=1,
            idempotency_key="narrated-render",
            target="render",
            asset_run_id=asset_run.id,
            storyboard=storyboard(recording),
        ),
        actor="operator",
    )
    monkeypatch.setattr(
        "katcha.editorial.render.render_editorial",
        lambda manifest: RenderResult(
            manifest.output_key,
            manifest.output_duration_seconds,
            {"verified": True, "composition": "Editorial"},
        ),
    )
    assert render_project(str(row.id), 1)["status"] == "completed"
    review_render(
        scout.channel_profile_id,
        scout.project_id,
        row.id,
        ReviewEditorialRender(
            idempotency_key="approve",
            expected_revision=1,
            expected_review_sequence=0,
            decision="approve",
        ),
        actor="operator",
    )
    assert review_status(scout.channel_profile_id, scout.project_id, row.id)["status"] == "approve"
    narration.revoke_narration(
        scout.channel_profile_id, scout.project_id, uuid.UUID(recording["id"]), actor="operator"
    )
    assert (
        review_status(scout.channel_profile_id, scout.project_id, row.id)["status"] == "invalidated"
    )
    with pytest.raises(EditorialConflict, match="removed"):
        compile_recording(project, recording, asset_run)


def test_narration_api_channel_scope_permissions_and_bounded_upload(project, saved):
    client, channel, other = saved
    scout, store = project
    url = f"/v1/channels/{channel}/editorial-projects/{scout.project_id}/narration"
    params = dict(revision=1, beat_id="beat-1", idempotency_key="api", permitted_use="true")
    response = client.post(url, params=params, content=wav(), headers={"Content-Type": "audio/wav"})
    assert response.status_code == 201, response.text
    assert response.json()["actor"] == "control-principal:editor"
    assert (
        client.get(url, params={"revision": 1}).json()["recordings"][0]["id"]
        == response.json()["id"]
    )
    assert (
        client.post(
            url,
            params=params,
            content=wav(),
            headers={
                "Authorization": "Bearer editorial-reader-token-00001",
            },
        ).status_code
        == 403
    )
    assert (
        client.get(url.replace(str(channel), str(other)), params={"revision": 1}).status_code == 403
    )
    assert (
        client.post(url, params={**params, "permitted_use": "false"}, content=wav()).status_code
        == 422
    )
    assert (
        client.post(url, params=params, content=b"x" * (narration.MAX_AUDIO_BYTES + 1)).status_code
        == 413
    )
    assert store.put_bytes.call_count == 1


def test_same_text_cannot_reuse_recording_across_revision(project):
    recording = upload(project)
    scout, _ = project
    from test_editorial_projects import draft

    with (
        db.session_scope() as session,
        pytest.raises(EditorialConflict, match="another script revision"),
    ):
        narration.resolve_narration(
            session,
            scout.channel_profile_id,
            scout.project_id,
            2,
            EditorialDraft.model_validate(draft()),
            storyboard(recording),
        )


def test_narration_migration_roundtrip():
    import importlib.util

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect

    spec = importlib.util.spec_from_file_location(
        "narration_migration", "migrations/versions/0053_editorial_narration.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        module.op = Operations(MigrationContext.configure(connection))
        module.upgrade()
        assert "editorial_narration" in inspect(connection).get_table_names()
        module.downgrade()
        assert "editorial_narration" not in inspect(connection).get_table_names()
    engine.dispose()


def test_silent_manifest_keeps_pre_narration_serialization():
    from test_editorial_visual_compiler import compile_plan, plan

    result = compile_plan(plan())
    assert result.version == "editorial-render-v1"
    assert "narration" not in result.model_dump(mode="json")
    assert "narration" not in result.model_dump_json()
