"""Synthetic images exercise intake, immutable versioned rendering and permission recovery."""

import hashlib
import io
import json
import uuid
from unittest.mock import Mock

import pytest
from PIL import Image, PngImagePlugin
from test_editorial_assets import scripted_project
from test_editorial_projects import root
from test_editorial_projects import saved as _saved

from katcha import db
from katcha.acquisition_models import DiscoveryCandidate, RightsAssessment
from katcha.editorial import images
from katcha.editorial.run_schemas import StartEditorialRun
from katcha.editorial.visual_compiler import compile_project_visuals
from katcha.editorial.visual_schemas import DirectionResult, StoryboardPlan
from katcha.editorial_models import EditorialImage, EditorialProject
from katcha.models import Clip, ClipFeature, SourceItem
from katcha.services.editorial_projects import EditorialConflict, EditorialNotFound
from katcha.services.editorial_runs import checkpoint, start_run

saved = _saved


def png(size=(320, 180), *, color="red"):
    data = io.BytesIO()
    info = PngImagePlugin.PngInfo()
    info.add_text("comment", "untrusted metadata")
    Image.new("RGBA", size, color).save(data, "PNG", pnginfo=info)
    return data.getvalue()


@pytest.fixture
def still(saved, monkeypatch):
    channel, project = scripted_project(saved)
    objects = {}
    store = Mock()
    store.put_bytes.side_effect = lambda data, key, **kwargs: objects.update({key: data})
    monkeypatch.setattr(images, "ObjectStore", lambda: store)
    request = images.ImageUpload(
        revision=1,
        beat_id="beat-1",
        idempotency_key="image",
        title="Original art",
        source_reference="Owned artwork",
        use_note="Created and cleared by operator",
        permitted_use=True,
        illustration=True,
    )
    return channel, project, request, objects, store


def upload(still, **changes):
    channel, project, request, _, _ = still
    return images.import_image(
        channel, project, request=request.model_copy(update=changes), data=png(), actor="test"
    )


def plan(identity):
    return StoryboardPlan(
        presentation_mode="captioned_silent",
        beats=[
            {"beat_id": "beat-1", "layout": "image", "image_id": identity},
        ],
    )


def test_intake_strips_metadata_replays_and_keeps_revocation(still):
    channel, project, _, objects, store = still
    first = upload(still)
    assert upload(still) == first
    assert first["width"] == 320 and first["height"] == 180
    assert len(objects) == 1
    with Image.open(io.BytesIO(next(iter(objects.values())))) as image:
        assert image.info == {}
        assert image.mode == "RGBA"
    store.put_bytes.assert_called_once()
    images.revoke_image(channel, project, first["id"], actor="test")
    assert upload(still)["status"] == "revoked"
    assert len(images.list_images(channel, project, 1)["images"]) == 1
    with pytest.raises(EditorialConflict):
        upload(still, use_note="Different permission")


@pytest.mark.parametrize("bad", [b"", b"<svg><script>bad()</script></svg>", b"not an image"])
def test_unsupported_images_rejected(bad):
    with pytest.raises(ValueError):
        images.normalize_image(bad)


def test_animation_and_oversize_dimensions_rejected():
    with pytest.raises(ValueError, match="8192"):
        images.normalize_image(png((8193, 1)))
    data = io.BytesIO()
    Image.new("RGB", (2, 2), "red").save(
        data, "PNG", save_all=True, append_images=[Image.new("RGB", (2, 2), "blue")]
    )
    with pytest.raises(ValueError):
        images.normalize_image(data.getvalue())


def test_permission_and_saved_revision_are_required_before_storage(still):
    with pytest.raises(ValueError, match="permission"):
        upload(still, permitted_use=False)
    with pytest.raises(EditorialConflict, match="beat"):
        upload(still, beat_id="missing")
    channel, project, _, _, store = still
    with db.session_scope() as session:
        session.get(EditorialProject, project).revision = 2
    with pytest.raises(EditorialConflict, match="Script changed"):
        upload(still)
    store.put_bytes.assert_not_called()


def test_storage_failure_retry_and_script_change_during_upload(still):
    channel, project, _, _, store = still
    original = store.put_bytes.side_effect
    store.put_bytes.side_effect = TimeoutError("storage response lost")
    with pytest.raises(TimeoutError):
        upload(still)
    with db.session_scope() as session:
        assert session.query(EditorialImage).count() == 0
    store.put_bytes.side_effect = original
    assert upload(still)["status"] == "active"

    def changed(*args, **kwargs):
        original(*args, **kwargs)
        with db.session_scope() as session:
            session.get(EditorialProject, project).revision = 2

    store.put_bytes.side_effect = changed
    with pytest.raises(EditorialConflict):
        upload(still, idempotency_key="second")
    with db.session_scope() as session:
        assert session.query(EditorialImage).count() == 1


def source_frame_fixture(still):
    channel, project, _, _, store = still
    source_url = "https://www.youtube.com/watch?v=source-frame"
    scout = start_run(
        channel,
        project,
        StartEditorialRun(
            expected_revision=1,
            idempotency_key="source-frame-scout",
            target="assets",
        ),
        actor="test",
    )
    checkpoint(
        str(scout.id),
        1,
        status="completed",
        stage="asset_candidates_ready",
        artifacts={
            "asset_scout": {
                "requests": [],
                "candidates": [
                    {
                        "id": "asset",
                        "medium": "video",
                        "url": source_url,
                        "title": "Cleared supporting footage",
                        "beat_id": "beat-1",
                        "claim_ids": ["claim-1"],
                    }
                ],
            }
        },
    )
    acquired = start_run(
        channel,
        project,
        StartEditorialRun(
            expected_revision=1,
            idempotency_key="source-frame-acquire",
            target="acquire_assets",
            scout_run_id=scout.id,
            asset_candidate_ids=["asset"],
        ),
        actor="test",
    )

    clip_id = uuid.uuid4()
    source_id = uuid.uuid4()
    candidate_id = uuid.uuid4()
    clip_sha = "f" * 64
    prefix = f"analysis/{clip_sha[:2]}/{clip_sha}/"
    frame_keys = [
        f"{prefix}frames/frame-001.jpg",
        f"{prefix}frames/frame-002.jpg",
        f"{prefix}frames/frame-003.jpg",
    ]
    with db.session_scope() as session:
        session.add(
            Clip(
                id=clip_id,
                sha256=clip_sha,
                storage_key="raw/source-frame.mp4",
                duration_seconds=10,
                width=1920,
                height=1080,
            )
        )
        session.add(
            SourceItem(
                id=source_id,
                source_url=source_url,
                canonical_url=source_url,
                platform="youtube",
                status="ready",
                clip_id=clip_id,
                source_metadata={
                    "channel_profile_id": str(channel),
                    "discovery_candidate_id": str(candidate_id),
                },
            )
        )
        session.add(
            DiscoveryCandidate(
                id=candidate_id,
                source_item_id=source_id,
                adapter_key="editorial_scout",
                platform="youtube",
                source_url=source_url,
                canonical_url=source_url,
                candidate_metadata={"channel_profile_id": str(channel)},
            )
        )
        session.add(
            RightsAssessment(
                discovery_candidate_id=candidate_id,
                version=1,
                production_eligible=True,
                actor="test",
            )
        )
        session.add(
            ClipFeature(
                clip_id=clip_id,
                contact_sheet_key=f"{prefix}contact-sheet.jpg",
                keyframe_keys=frame_keys,
            )
        )
    checkpoint(
        str(acquired.id),
        1,
        status="completed",
        stage="assets_acquired_for_review",
        artifacts={
            "acquired_assets": {
                "asset": {
                    "candidate_id": "asset",
                    "clip_id": str(clip_id),
                    "storage_key": "raw/source-frame.mp4",
                    "sha256": clip_sha,
                    "duration_seconds": 10.0,
                    "width": 1920,
                    "height": 1080,
                    "source_url": source_url,
                    "purpose": "review",
                    "discovery_candidate_id": str(candidate_id),
                }
            },
            "requires_rights_review": False,
        },
    )
    store.get_bytes.return_value = png((640, 360), color="blue")
    request = images.SourceFrameCapture(
        revision=1,
        beat_id="beat-1",
        asset_run_id=acquired.id,
        candidate_id="asset",
        frame_index=1,
    )
    return channel, project, request, candidate_id, frame_keys[1], store


def test_cleared_source_frame_is_idempotent_and_rights_bound(still):
    channel, project, request, candidate_id, frame_key, store = source_frame_fixture(still)

    first = images.capture_source_frame(
        channel,
        project,
        request=request,
        actor="test",
    )
    assert first["illustration"] is False
    assert first["width"] == 640 and first["height"] == 360
    assert "frame 2" in first["title"]
    assert images.capture_source_frame(
        channel,
        project,
        request=request,
        actor="test",
    ) == first
    store.get_bytes.assert_called_once_with(frame_key)
    store.put_bytes.assert_called_once()

    manifest = compile_project_visuals(channel, project, 1, None, plan(first["id"]))
    assert manifest.images[0].image_id == first["id"]

    with db.session_scope() as session:
        row = session.get(EditorialImage, first["id"])
        assert row.source_metadata["kind"] == "source_frame"
        assert row.source_metadata["frame_index"] == 1
        session.add(
            RightsAssessment(
                discovery_candidate_id=candidate_id,
                version=2,
                production_eligible=False,
                actor="test",
            )
        )
    with pytest.raises(EditorialConflict, match="no longer has production-eligible clearance"):
        compile_project_visuals(channel, project, 1, None, plan(first["id"]))
    with pytest.raises(EditorialConflict, match="current clearance"):
        images.capture_source_frame(
            channel,
            project,
            request=request,
            actor="test",
        )


def test_source_frame_capture_api_requires_editor(still):
    channel, project, request, _, _, _ = source_frame_fixture(still)
    client = saved[0]
    url = f"{root(channel)}/{project}/images/source-frame"
    payload = request.model_dump(mode="json")
    response = client.post(url, json=payload)
    assert response.status_code == 201, response.text
    reader = {"Authorization": "Bearer editorial-reader-token-00001"}
    assert client.post(url, json=payload, headers=reader).status_code == 403


def test_image_only_render_compiles_and_revocation_blocks(still):
    channel, project, _, _, _ = still
    first = upload(still)
    storyboard = plan(first["id"])
    manifest = compile_project_visuals(channel, project, 1, None, storyboard)
    assert manifest.version == "editorial-render-v6"
    assert manifest.brand is not None
    assert manifest.images[0].illustration is True
    assert manifest.output_duration_seconds == 8
    assert compile_project_visuals(channel, project, 1, None, storyboard) == manifest
    row = start_run(
        channel,
        project,
        StartEditorialRun(
            expected_revision=1, idempotency_key="render", target="render", storyboard=storyboard
        ),
        actor="test",
    )
    assert row.options["asset_run_id"] is None
    images.revoke_image(channel, project, first["id"], actor="test")
    with pytest.raises(EditorialConflict, match="removed"):
        compile_project_visuals(channel, project, 1, None, storyboard)


def test_wrong_beat_revision_or_channel_cannot_reuse_image(still, saved):
    channel, project, _, _, _ = still
    first = upload(still)
    with pytest.raises(EditorialNotFound):
        images.revoke_image(saved[2], project, first["id"], actor="test")
    with db.session_scope() as session:
        session.get(EditorialImage, first["id"]).beat_id = "other-beat"
    with pytest.raises(EditorialConflict, match="another script"):
        compile_project_visuals(channel, project, 1, None, plan(first["id"]))


def test_authenticated_upload_api_and_permission_replay(still, saved):
    channel, project, request, _, _ = still
    client = saved[0]
    url = f"{root(channel)}/{project}/images"
    metadata = request.model_dump(mode="json")
    first = client.post(url, params=metadata, content=png())
    assert first.status_code == 201, first.text
    assert client.post(url, params=metadata, content=png()).json() == first.json()
    assert client.get(url, params={"revision": 1}).json()["images"][0]["title"] == request.title
    reader = {"Authorization": "Bearer editorial-reader-token-00001"}
    assert client.post(url, params=metadata, content=png(), headers=reader).status_code == 403
    assert (
        client.get(f"{root(saved[2])}/{project}/images", params={"revision": 1}).status_code == 403
    )
    assert (
        client.post(url, params={**metadata, "permitted_use": False}, content=png()).status_code
        == 422
    )


def test_direction_provider_schema_remains_compatible_with_saved_receipts():
    encoded = json.dumps(DirectionResult.model_json_schema(), sort_keys=True).encode()
    assert hashlib.sha256(encoded).hexdigest() == (
        "f122bfd38e74c4390913ddc587b2e39c30ec6767bb7882128391c7e9055c5d44"
    )


def test_image_migration_roundtrip():
    import importlib.util

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect

    spec = importlib.util.spec_from_file_location(
        "image_migration", "migrations/versions/0054_editorial_images.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        module.op = Operations(MigrationContext.configure(connection))
        module.upgrade()
        assert "editorial_images" in inspect(connection).get_table_names()
        module.downgrade()
        assert "editorial_images" not in inspect(connection).get_table_names()
    engine.dispose()


def test_narrated_image_render_and_approval_revocation(still, monkeypatch):
    from test_editorial_narration import wav

    from katcha.editorial import narration
    from katcha.editorial.render import render_project
    from katcha.editorial.review_schemas import ReviewEditorialRender
    from katcha.rendering.client import RenderResult
    from katcha.services.editorial_reviews import review_render, review_status

    channel, project, _, _, _ = still
    first = upload(still)
    monkeypatch.setattr(narration, "ObjectStore", lambda: Mock())
    recording = narration.import_narration(
        channel,
        project,
        revision=1,
        beat_id="beat-1",
        idempotency_key="voice",
        audio=wav(),
        actor="test",
        permitted_use=True,
    )
    storyboard = StoryboardPlan(
        presentation_mode="narrated",
        beats=plan(first["id"]).beats,
        narration_ids={"beat-1": recording["id"]},
    )
    run = start_run(
        channel,
        project,
        StartEditorialRun(
            expected_revision=1,
            idempotency_key="voiced-image",
            target="render",
            storyboard=storyboard,
        ),
        actor="test",
    )
    monkeypatch.setattr(
        "katcha.editorial.render.render_editorial",
        lambda manifest: RenderResult(
            manifest.output_key,
            manifest.output_duration_seconds,
            {"verified": True, "composition": "Editorial"},
        ),
    )
    assert render_project(str(run.id), 1)["status"] == "completed"
    manifest = compile_project_visuals(channel, project, 1, None, storyboard)
    assert manifest.version == "editorial-render-v6"
    assert manifest.brand is not None
    assert manifest.output_duration_seconds == 61 / 30
    review_render(
        channel,
        project,
        run.id,
        ReviewEditorialRender(
            expected_revision=1,
            expected_review_sequence=0,
            idempotency_key="approve",
            decision="approve",
            note="Synthetic still review",
        ),
        actor="test",
    )
    assert review_status(channel, project, run.id)["status"] == "approve"
    images.revoke_image(channel, project, first["id"], actor="test")
    assert review_status(channel, project, run.id)["status"] == "invalidated"


def test_comparison_annotations_compile_both_receipts_and_revoke(still):
    from katcha.editorial.visual_schemas import EditorialRenderManifest

    channel, project, *_ = still
    first = upload(still)
    second = upload(still, idempotency_key="second", title="Second image")
    storyboard = StoryboardPlan(
        presentation_mode="captioned_silent",
        beats=[
            {
                "beat_id": "beat-1",
                "layout": "image_comparison",
                "image_ids": [first["id"], second["id"]],
                "image_push_in": 1.1,
                "overlays": [
                    {
                        "kind": "circle",
                        "media_index": 1,
                        "region": {"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4},
                    }
                ],
            }
        ],
    )
    result = compile_project_visuals(channel, project, 1, None, storyboard)
    assert result.version == "editorial-render-v6"
    assert result.brand is not None
    assert [image.image_id for image in result.images] == [first["id"], second["id"]]
    assert EditorialRenderManifest.model_validate_json(result.model_dump_json()) == result
    assert result.timeline[0].overlays[0].media_index == 1
    images.revoke_image(channel, project, second["id"], actor="test")
    with pytest.raises(EditorialConflict, match="removed"):
        compile_project_visuals(channel, project, 1, None, storyboard)


def test_single_image_annotation_versions_without_changing_old_serialization(still):
    from pydantic import ValidationError

    from katcha.editorial.visual_schemas import EditorialRenderManifest

    channel, project, *_ = still
    storyboard = plan(upload(still)["id"])
    original = compile_project_visuals(channel, project, 1, None, storyboard)
    assert "image_ids" not in original.model_dump()["timeline"][0]
    data = storyboard.model_dump(mode="json")
    data["beats"][0]["overlays"] = [
        {
            "kind": "highlight",
            "region": {
                "x": 0,
                "y": 0,
                "width": 1,
                "height": 1,
            },
        }
    ]
    annotated = compile_project_visuals(
        channel, project, 1, None, StoryboardPlan.model_validate(data)
    )
    assert annotated.version == "editorial-render-v6"
    legacy = annotated.model_dump()
    legacy.pop("brand", None)
    legacy["version"] = "editorial-render-v3"
    with pytest.raises(ValidationError, match="version 4"):
        EditorialRenderManifest.model_validate(legacy)
    assert compile_project_visuals(channel, project, 1, None, storyboard) == original


@pytest.mark.parametrize("change", ["duplicate", "missing", "index", "bounds", "single"])
def test_invalid_image_comparisons_rejected(change):
    import uuid

    from pydantic import ValidationError

    from katcha.editorial.visual_schemas import VisualBeat

    ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    value = {
        "beat_id": "beat",
        "layout": "image_comparison",
        "image_ids": ids,
        "overlays": [
            {
                "kind": "arrow",
                "media_index": 1,
                "region": {"x": 0, "y": 0, "width": 0.5, "height": 0.5},
            }
        ],
    }
    if change == "duplicate":
        value["image_ids"] = [ids[0], ids[0]]
    elif change == "missing":
        value["image_ids"] = [ids[0]]
    elif change == "index":
        value["overlays"][0]["media_index"] = 2
    elif change == "bounds":
        value["overlays"][0]["region"]["x"] = 0.9
    else:
        value["layout"] = "image"
        value["image_id"] = ids[0]
    with pytest.raises(ValidationError):
        VisualBeat.model_validate(value)


@pytest.mark.parametrize("field,value", [("beat_id", "other-beat"), ("revision", 2)])
def test_second_comparison_image_must_match_current_beat(still, field, value):
    channel, project, *_ = still
    first = upload(still)
    second = upload(still, idempotency_key="second")
    storyboard = StoryboardPlan(
        presentation_mode="captioned_silent",
        beats=[
            {
                "beat_id": "beat-1",
                "layout": "image_comparison",
                "image_ids": [first["id"], second["id"]],
            }
        ],
    )
    with db.session_scope() as session:
        setattr(session.get(EditorialImage, second["id"]), field, value)
    with pytest.raises(EditorialConflict, match="another script beat or revision"):
        compile_project_visuals(channel, project, 1, None, storyboard)
