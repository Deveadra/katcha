from __future__ import annotations

import uuid

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from katcha import db
from katcha.editorial.project_schemas import CreateEditorialProject, SaveEditorialDraft
from katcha.editorial_models import ProjectMediaItem
from katcha.intelligence_models import ChannelProfile
from katcha.models import Clip, SourceItem
from katcha.services.editorial_media_bin import (
    add_project_media_item,
    get_project_media_item,
    list_project_media_items,
)
from katcha.services.editorial_projects import (
    EditorialConflict,
    EditorialNotFound,
    create_project,
    save_draft,
)


@pytest.fixture
def media_bin(monkeypatch):
    db.load_model_metadata()
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    db.Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=engine, expire_on_commit=False))
    channels = (uuid.uuid4(), uuid.uuid4())
    with db.session_scope() as session:
        for channel_id in channels:
            session.add(
                ChannelProfile(
                    id=channel_id,
                    youtube_connection_id=uuid.uuid4(),
                    status="active",
                    timezone="UTC",
                    profile_metadata={},
                )
            )
    projects = []
    for index, channel_id in enumerate(channels):
        project = create_project(
            channel_id,
            CreateEditorialProject.model_validate(
                {
                    "idempotency_key": f"project-{index}",
                    "brief": {"prompt": "Fixture", "source_urls": ["https://example.org"]},
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
                    "idempotency_key": f"revision-{index}",
                    "draft": {"version": "editorial-draft-v1", "script": []},
                }
            ),
            actor="test",
        )
        projects.append((project, revision))
    yield engine, channels, projects
    engine.dispose()


def test_reference_is_durable_and_idempotent_without_claiming_rights(media_bin):
    _, (channel, _), ((project, revision), _) = media_bin
    kwargs = {
        "request_key": "source-link-1",
        "actor": "test",
        "kind": "video",
        "external_reference_url": "https://video.example/watch/one",
        "requirement_ids": ["visual-1"],
        "provenance": {"provider": "operator", "source_url": "https://video.example/watch/one"},
    }
    first = add_project_media_item(channel, project.id, revision.revision, **kwargs)
    replay = add_project_media_item(channel, project.id, revision.revision, **kwargs)

    assert replay.id == first.id
    assert first.project_id == project.id
    assert first.revision == revision.revision
    assert first.status == "reference_only"
    assert first.rights_status == "reference_only"
    assert first.requirement_ids == ["visual-1"]
    assert list_project_media_items(channel, project.id)[0].id == first.id
    assert get_project_media_item(channel, project.id, first.id).external_reference_url == kwargs[
        "external_reference_url"
    ]
    with pytest.raises(EditorialConflict, match="identity"):
        add_project_media_item(
            channel,
            project.id,
            revision.revision,
            **{**kwargs, "external_reference_url": "https://video.example/watch/two"},
        )


def test_existing_clip_is_referenced_once_and_channel_isolation_is_enforced(media_bin):
    engine, (channel, other_channel), ((project, revision), (other_project, other_revision)) = (
        media_bin
    )
    clip_id = uuid.uuid4()
    with db.session_scope() as session:
        session.add(
            Clip(
                id=clip_id,
                sha256=uuid.uuid4().hex * 2,
                storage_key="canonical/fixture.mp4",
                status="scored",
            )
        )
        session.add(
            SourceItem(
                source_url="https://owned.example/fixture.mp4",
                canonical_url="https://owned.example/fixture.mp4",
                platform="web",
                clip_id=clip_id,
                source_metadata={"channel_profile_id": str(channel)},
            )
        )

    item = add_project_media_item(
        channel,
        project.id,
        revision.revision,
        request_key="owned-clip-1",
        actor="test",
        kind="video",
        clip_id=clip_id,
        provenance={"source_url": "https://owned.example/fixture.mp4"},
    )
    assert item.status == "acquired"
    assert item.rights_status == "not_assessed"
    with db.session_scope() as session:
        assert session.scalar(select(ProjectMediaItem.clip_id)) == clip_id
        assert (
            session.scalar(select(Clip.storage_key).where(Clip.id == clip_id))
            == "canonical/fixture.mp4"
        )

    with pytest.raises(EditorialConflict, match="not available"):
        add_project_media_item(
            other_channel,
            other_project.id,
            other_revision.revision,
            request_key="cross-channel-clip",
            actor="test",
            kind="video",
            clip_id=clip_id,
        )
    with pytest.raises(EditorialNotFound):
        list_project_media_items(other_channel, project.id)
    with pytest.raises(EditorialNotFound):
        get_project_media_item(other_channel, project.id, item.id)
    assert engine.dialect.name == "sqlite"


def test_links_and_ready_state_fail_closed(media_bin):
    _, (channel, _), ((project, revision), _) = media_bin
    with pytest.raises(ValueError, match="invalid source reference"):
        add_project_media_item(
            channel,
            project.id,
            revision.revision,
            request_key="invalid-scheme",
            actor="test",
            kind="video",
            external_reference_url="file:///etc/passwd",
        )
    with pytest.raises(ValueError, match="production-cleared"):
        add_project_media_item(
            channel,
            project.id,
            revision.revision,
            request_key="not-ready",
            actor="test",
            kind="video",
            external_reference_url="https://example.org/video",
            status="ready",
            rights_status="production_cleared",
        )


def test_media_bin_migration_is_additive_and_model_columns_match():
    config = Config("alembic.ini")
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == ["0059_acquisition_receipts"]
    revision = script.get_revision("0058_project_media_bin")
    assert revision.down_revision == "0057_editorial_image_lineage"

    db.load_model_metadata()
    expected = {
        "id",
        "project_id",
        "revision",
        "channel_profile_id",
        "clip_id",
        "external_reference_url",
        "request_id",
        "request_digest",
        "kind",
        "status",
        "rights_status",
        "rights_assessment_id",
        "rights_assessment_version",
        "requirement_ids",
        "searchable_tags",
        "provenance",
        "source_metadata",
        "source_in_seconds",
        "source_out_seconds",
        "notes",
        "actor",
        "created_at",
        "last_verified_at",
    }
    assert set(db.Base.metadata.tables["project_media_items"].columns.keys()) == expected
    assert {"id", "channel_profile_id"} <= set(
        db.Base.metadata.tables["editorial_projects"].columns.keys()
    )
    assert script.get_revision("0059_acquisition_receipts").down_revision == (
        "0058_project_media_bin"
    )
    assert "acquisition_receipts" in db.Base.metadata.tables
