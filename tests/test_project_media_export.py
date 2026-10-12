from __future__ import annotations

import hashlib
import io
import json
import uuid
import zipfile
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from katcha import db
from katcha.acquisition_models import DiscoveryCandidate, RightsAssessment, RightsEvidence
from katcha.editorial.media_intake.export import build_project_media_export
from katcha.editorial.project_schemas import CreateEditorialProject, SaveEditorialDraft
from katcha.editorial_models import (
    AcquisitionReceipt,
    MediaAssetValidation,
    ProjectMediaItem,
)
from katcha.intelligence_models import ChannelProfile
from katcha.models import Clip, SourceItem
from katcha.services.editorial_projects import create_project, save_draft


class MemoryStore:
    def __init__(self, objects: dict[str, bytes]):
        self.objects = objects

    def iter_bytes(self, key: str, chunk_size: int = 1024 * 1024):
        value = self.objects[key]
        for start in range(0, len(value), chunk_size):
            yield value[start : start + chunk_size]


@pytest.fixture
def export_context(monkeypatch):
    db.load_model_metadata()
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    db.Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=engine, expire_on_commit=False))
    channel_id, other_channel_id = uuid.uuid4(), uuid.uuid4()
    with db.session_scope() as session:
        session.add_all(
            [
                ChannelProfile(
                    id=value,
                    youtube_connection_id=uuid.uuid4(),
                    status="active",
                    timezone="UTC",
                    profile_metadata={},
                )
                for value in (channel_id, other_channel_id)
            ]
        )
    project = create_project(
        channel_id,
        CreateEditorialProject.model_validate(
            {
                "idempotency_key": "export-project",
                "brief": {
                    "prompt": "Export fixture",
                    "source_urls": ["https://source.example/fixture"],
                },
            }
        ),
        actor="test",
    )
    revision = save_draft(
        channel_id,
        project.id,
        SaveEditorialDraft.model_validate(
            {
                "expected_revision": 0,
                "idempotency_key": "export-revision",
                "draft": {"version": "editorial-draft-v1", "script": []},
            }
        ),
        actor="test",
    )
    yield engine, channel_id, other_channel_id, project.id, revision.revision
    engine.dispose()


def add_exportable_item(engine, channel_id, project_id, revision):
    data = b"export fixture bytes"
    digest = hashlib.sha256(data).hexdigest()
    candidate_id, source_id, clip_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    receipt_id, assessment_id, item_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with db.session_scope() as session:
        session.add(
            Clip(
                id=clip_id,
                sha256=digest,
                storage_key="internal/private-storage-key",
                extension="mp4",
                size_bytes=len(data),
                status="scored",
                media_metadata={"codec": "h264", "api_key": "remove-me"},
            )
        )
        session.add(
            SourceItem(
                id=source_id,
                source_url="https://source.example/watch?id=public-source",
                canonical_url="https://source.example/watch?id=public-source",
                platform="web",
                clip_id=clip_id,
                source_metadata={"channel_profile_id": str(channel_id)},
            )
        )
        session.add(
            DiscoveryCandidate(
                id=candidate_id,
                source_item_id=source_id,
                adapter_key="fixture",
                external_id="fixture-1",
                source_url="https://source.example/watch?id=public-source",
                canonical_url="https://source.example/watch?id=public-source",
                platform="web",
                candidate_metadata={"channel_profile_id": str(channel_id)},
            )
        )
        session.add(
            RightsAssessment(
                id=assessment_id,
                discovery_candidate_id=candidate_id,
                version=1,
                rights_basis="licensed",
                rights_lane="green",
                rights_gate="cleared",
                audio_status="cleared",
                originality_gate="cleared",
                production_eligible=True,
                operator_authorized=True,
                risk_flags=[],
                fair_use_factors={},
                assessment_metadata={},
                actor="test",
                reason="Test license",
            )
        )
        session.add(
            RightsEvidence(
                id=uuid.uuid4(),
                rights_assessment_id=assessment_id,
                evidence_type="license",
                source_url="https://licenses.example/terms?token=temporary-secret",
                content_sha256=digest,
                terms_version="v1",
                evidence_metadata={"license": "fixture", "password": "discard"},
            )
        )
        session.add(
            AcquisitionReceipt(
                id=receipt_id,
                channel_profile_id=channel_id,
                candidate_id=candidate_id,
                operation_key="export-operation",
                request_digest=hashlib.sha256(b"request").hexdigest(),
                status="succeeded",
                authorization_scope={
                    "provider": "fixture",
                    "operations": ["download"],
                    "evidence_refs": ["https://permission.example/grant?token=do-not-export"],
                },
                fetch_url_if_allowed="https://source.example/file?token=transient",
                request_time=datetime.now(UTC),
                response_time=datetime.now(UTC),
                bytes_received=len(data),
                sha256=digest,
                mime="video/mp4",
                media_probe={},
                object_store_key="internal/private-storage-key",
                download_terms={"license": "fixture"},
                cost_receipt={},
                safe_resume_cursor={},
            )
        )
        session.add(
            MediaAssetValidation(
                id=uuid.uuid4(),
                receipt_id=receipt_id,
                channel_profile_id=channel_id,
                probe_version=1,
                status="validated",
                media_type="video",
                expected_sha256=digest,
                actual_sha256=digest,
                mime="video/mp4",
                size_bytes=len(data),
                duration_seconds=1,
                width=16,
                height=16,
                probe={"codec": "h264"},
            )
        )
        session.add(
            ProjectMediaItem(
                id=item_id,
                project_id=project_id,
                revision=revision,
                channel_profile_id=channel_id,
                clip_id=clip_id,
                external_reference_url=None,
                request_id=uuid.uuid4(),
                request_digest=hashlib.sha256(b"item").hexdigest(),
                kind="video",
                status="ready",
                rights_status="production_cleared",
                rights_assessment_id=assessment_id,
                rights_assessment_version=1,
                requirement_ids=["scene-1"],
                searchable_tags=["fixture"],
                provenance={"source": "fixture"},
                source_metadata={"title": "Portable", "secret_key": "remove-me"},
                actor="test",
                notes="Fixture note",
            )
        )
    return data, digest, receipt_id


def test_export_is_reproducible_and_includes_only_verified_cleared_bytes(export_context):
    engine, channel_id, _, project_id, revision = export_context
    data, digest, _ = add_exportable_item(engine, channel_id, project_id, revision)
    store = MemoryStore({"internal/private-storage-key": data})
    first = build_project_media_export(channel_id, project_id, revision, store=store)
    second = build_project_media_export(channel_id, project_id, revision, store=store)
    assert first == second

    with zipfile.ZipFile(io.BytesIO(first)) as archive:
        names = archive.namelist()
        media_name = f"media/{digest[:2]}/{digest}.mp4"
        assert media_name in names
        assert archive.read(media_name) == data
        manifest = json.loads(archive.read("manifest.json"))
        links = archive.read("references/links.csv").decode()
        assert manifest["schema"] == "katcha-project-media-export-v1"
        assert manifest["items"][0]["rights_status"] == "production_cleared"
        assert (
            manifest["items"][0]["rights_assessment"]["evidence"][0]["evidence_type"] == "license"
        )
        assert "https://source.example/watch?id=public-source" in links
        for forbidden in (
            "private-storage-key",
            "transient",
            "do-not-export",
            "temporary-secret",
            "remove-me",
        ):
            assert forbidden not in json.dumps(manifest) + links


def test_export_gaps_unvalidated_or_changed_media_without_bytes(export_context):
    engine, channel_id, _, project_id, revision = export_context
    data, digest, _ = add_exportable_item(engine, channel_id, project_id, revision)
    store = MemoryStore({"internal/private-storage-key": b"changed bytes"})
    archive_bytes = build_project_media_export(channel_id, project_id, revision, store=store)
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        assert not any(name.startswith("media/") for name in archive.namelist())
        manifest = archive.read("manifest.json").decode()
        assert "stored_media_hash_changed" in manifest
        assert digest in manifest
    assert data


def test_export_never_includes_restricted_bytes(export_context):
    engine, channel_id, _, project_id, revision = export_context
    data, _, _ = add_exportable_item(engine, channel_id, project_id, revision)
    with db.session_scope() as session:
        item = session.scalar(
            select(ProjectMediaItem).where(ProjectMediaItem.project_id == project_id)
        )
        assert item is not None
        item.status = "denied"
        item.rights_status = "restricted"
    archive_bytes = build_project_media_export(
        channel_id,
        project_id,
        revision,
        store=MemoryStore({"internal/private-storage-key": data}),
    )
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        assert not any(name.startswith("media/") for name in archive.namelist())
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["items"][0]["gap_reason"] == "production_clearance_required"


def test_export_omits_bytes_when_clearance_has_been_superseded(export_context):
    engine, channel_id, _, project_id, revision = export_context
    data, _, _ = add_exportable_item(engine, channel_id, project_id, revision)
    with db.session_scope() as session:
        item = session.scalar(
            select(ProjectMediaItem).where(ProjectMediaItem.project_id == project_id)
        )
        assert item is not None and item.rights_assessment_id is not None
        previous = session.get(RightsAssessment, item.rights_assessment_id)
        assert previous is not None
        session.add(
            RightsAssessment(
                id=uuid.uuid4(),
                discovery_candidate_id=previous.discovery_candidate_id,
                version=2,
                rights_basis="licensed",
                rights_lane="yellow",
                rights_gate="review_required",
                audio_status="review_required",
                originality_gate="review_required",
                production_eligible=False,
                operator_authorized=False,
                risk_flags=["license_expired"],
                fair_use_factors={},
                assessment_metadata={},
                actor="test",
                reason="Updated review required",
            )
        )
    archive_bytes = build_project_media_export(
        channel_id,
        project_id,
        revision,
        store=MemoryStore({"internal/private-storage-key": data}),
    )
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        assert not any(name.startswith("media/") for name in archive.namelist())
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["items"][0]["gap_reason"] == "production_clearance_required"


def test_export_is_channel_scoped(export_context):
    _, _, other_channel_id, project_id, revision = export_context
    with pytest.raises(ValueError, match="not found in this channel"):
        build_project_media_export(other_channel_id, project_id, revision, store=MemoryStore({}))


def test_latest_rejected_validation_cannot_be_bypassed(export_context):
    engine, channel_id, _, project_id, revision = export_context
    data, digest, receipt_id = add_exportable_item(engine, channel_id, project_id, revision)
    with db.session_scope() as session:
        session.add(
            MediaAssetValidation(
                id=uuid.uuid4(),
                receipt_id=receipt_id,
                channel_profile_id=channel_id,
                probe_version=2,
                status="rejected",
                media_type="video",
                expected_sha256=digest,
                actual_sha256=digest,
                mime="video/mp4",
                size_bytes=len(data),
                probe={},
                error_category="content_probe_failed",
            )
        )
    result = build_project_media_export(
        channel_id, project_id, revision, store=MemoryStore({"internal/private-storage-key": data})
    )
    with zipfile.ZipFile(io.BytesIO(result)) as archive:
        assert not any(name.startswith("media/") for name in archive.namelist())
        assert "validated_acquisition_required" in archive.read("manifest.json").decode()


def test_archive_limit_includes_manifest_and_zip_headers(export_context, monkeypatch):
    import tempfile
    from pathlib import Path

    from katcha.editorial.media_intake import export

    _, channel_id, _, project_id, revision = export_context
    paths = []
    original = tempfile.NamedTemporaryFile

    def capture(*args, **kwargs):
        handle = original(*args, **kwargs)
        paths.append(Path(handle.name))
        return handle

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", capture)
    monkeypatch.setattr(export, "MAX_EXPORT_BYTES", 100)
    with pytest.raises(ValueError, match="archive size"):
        build_project_media_export(channel_id, project_id, revision, store=MemoryStore({}))
    assert paths and all(not path.exists() for path in paths)


def test_underreported_asset_is_streamed_with_actual_byte_limit(export_context, monkeypatch):
    from katcha.editorial.media_intake import export

    engine, channel_id, _, project_id, revision = export_context
    data, _, receipt_id = add_exportable_item(engine, channel_id, project_id, revision)
    with db.session_scope() as session:
        row = session.scalar(
            select(MediaAssetValidation).where(MediaAssetValidation.receipt_id == receipt_id)
        )
        row.size_bytes = 1
    monkeypatch.setattr(export, "MAX_ASSET_BYTES", 5)
    consumed = []

    class ChunkStore:
        def iter_bytes(self, key, chunk_size=1024 * 1024):
            for chunk in (b"1234", b"5678", b"never read"):
                consumed.append(chunk)
                yield chunk

        def get_bytes(self, *args, **kwargs):
            pytest.fail("Export must not materialize complete storage objects")

    result = build_project_media_export(channel_id, project_id, revision, store=ChunkStore())
    assert consumed == [b"1234", b"5678"]
    with zipfile.ZipFile(io.BytesIO(result)) as archive:
        assert not any(name.startswith("media/") for name in archive.namelist())
        assert "asset_size_limit" in archive.read("manifest.json").decode()


def test_clearance_superseded_during_storage_read_omits_bytes(export_context):
    engine, channel_id, _, project_id, revision = export_context
    data, _, _ = add_exportable_item(engine, channel_id, project_id, revision)

    class RevokingStore(MemoryStore):
        def revoke(self):
            with db.session_scope() as session:
                previous = session.scalar(select(RightsAssessment))
                session.add(
                    RightsAssessment(
                        id=uuid.uuid4(),
                        discovery_candidate_id=previous.discovery_candidate_id,
                        version=2,
                        production_eligible=False,
                        reason="Revoked during read",
                    )
                )

        def iter_bytes(self, key, chunk_size=1024 * 1024):
            self.revoke()
            yield from super().iter_bytes(key, chunk_size)

        def get_bytes(self, key, *, max_bytes=None):
            self.revoke()
            return self.objects[key]

    result = build_project_media_export(
        channel_id,
        project_id,
        revision,
        store=RevokingStore({"internal/private-storage-key": data}),
    )
    with zipfile.ZipFile(io.BytesIO(result)) as archive:
        assert not any(name.startswith("media/") for name in archive.namelist())
        assert "production_clearance_required" in archive.read("manifest.json").decode()


def test_export_requires_receipt_bytes_to_match_canonical_clip(export_context):
    engine, channel_id, _, project_id, revision = export_context
    data, _, _ = add_exportable_item(engine, channel_id, project_id, revision)
    with db.session_scope() as session:
        clip = session.scalar(select(Clip))
        clip.sha256 = hashlib.sha256(b"a different canonical clip").hexdigest()
    result = build_project_media_export(
        channel_id, project_id, revision, store=MemoryStore({"internal/private-storage-key": data})
    )
    with zipfile.ZipFile(io.BytesIO(result)) as archive:
        assert not any(name.startswith("media/") for name in archive.namelist())
        assert "clip_content_hash_mismatch" in archive.read("manifest.json").decode()


def test_file_export_streams_archive_and_reuses_duplicate_blob(export_context, monkeypatch):
    from pathlib import Path

    from katcha.editorial.media_intake.export import build_project_media_export_file
    from katcha.services.editorial_media_bin import add_project_media_item

    engine, channel_id, _, project_id, revision = export_context
    data, _, _ = add_exportable_item(engine, channel_id, project_id, revision)
    with db.session_scope() as session:
        first = session.scalar(select(ProjectMediaItem))
    add_project_media_item(
        channel_id,
        project_id,
        revision,
        request_key="second-reference",
        actor="test",
        kind="video",
        clip_id=first.clip_id,
        status="ready",
        rights_status="production_cleared",
        rights_assessment_id=first.rights_assessment_id,
        rights_assessment_version=1,
    )
    reads = []

    class CountingStore(MemoryStore):
        def iter_bytes(self, key, chunk_size=1024 * 1024):
            reads.append(key)
            yield from super().iter_bytes(key, chunk_size)

    def no_materialization(path):
        pytest.fail("File export must not materialize archive bytes")

    monkeypatch.setattr(Path, "read_bytes", no_materialization)
    archive_path = build_project_media_export_file(
        channel_id,
        project_id,
        revision,
        store=CountingStore({"internal/private-storage-key": data}),
    )
    try:
        with zipfile.ZipFile(archive_path) as archive:
            assert len([name for name in archive.namelist() if name.startswith("media/")]) == 1
            assert len(json.loads(archive.read("manifest.json"))["items"]) == 2
        assert reads == ["internal/private-storage-key"]
    finally:
        archive_path.unlink(missing_ok=True)


def test_manifest_size_limit_is_enforced_before_returning_archive(export_context, monkeypatch):
    from katcha.editorial.media_intake import export

    _, channel_id, _, project_id, revision = export_context
    monkeypatch.setattr(export, "MAX_METADATA_BYTES", 32)
    with pytest.raises(ValueError, match="metadata exceeds"):
        build_project_media_export(channel_id, project_id, revision, store=MemoryStore({}))
