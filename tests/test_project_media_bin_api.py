from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from katcha import db
from katcha.api.editorial_projects import (
    media_bin_add,
    media_bin_detail,
    media_bin_export,
    media_bin_list,
)
from katcha.editorial.media_bin_schemas import AddProjectMediaItemRequest
from katcha.editorial.project_schemas import CreateEditorialProject, SaveEditorialDraft
from katcha.intelligence_models import ChannelProfile
from katcha.models import Clip, SourceItem
from katcha.services.editorial_projects import create_project, save_draft


@pytest.fixture
def media_bin_api(monkeypatch):
    db.load_model_metadata()
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    db.Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=engine, expire_on_commit=False))
    channel_id, other_channel_id = uuid.uuid4(), uuid.uuid4()
    with db.session_scope() as session:
        for value in (channel_id, other_channel_id):
            session.add(
                ChannelProfile(
                    id=value,
                    youtube_connection_id=uuid.uuid4(),
                    status="active",
                    timezone="UTC",
                    profile_metadata={},
                )
            )
    project = create_project(
        channel_id,
        CreateEditorialProject.model_validate(
            {
                "idempotency_key": "project",
                "brief": {"prompt": "Fixture", "source_urls": ["https://example.org/source"]},
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
                "idempotency_key": "revision",
                "draft": {"version": "editorial-draft-v1", "script": []},
            }
        ),
        actor="test",
    )
    yield channel_id, other_channel_id, project.id, revision.revision
    engine.dispose()


def authorized_request(
    channel_id: uuid.UUID, *, scopes: set[str] | None = None
) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/v1/test",
        "headers": [],
        "query_string": b"",
        "state": {
            "control_actor": "test",
            "control_principal_name": "test-editor",
            "control_scopes": set(scopes or {"ai:read", "production:create"}),
            "control_channel_profile_ids": {str(channel_id)},
        },
    }
    return Request(scope)


def add_body(revision: int, url: str) -> AddProjectMediaItemRequest:
    return AddProjectMediaItemRequest.model_validate(
        {
            "revision": revision,
            "kind": "video",
            "external_reference_url": url,
            "requirement_ids": ["visual-1"],
            "searchable_tags": ["trailer"],
        }
    )


def test_external_link_save_is_reference_only_and_replays_deterministically(media_bin_api):
    channel, _, project_id, revision = media_bin_api
    request = authorized_request(channel)
    body = add_body(
        revision,
        "https://video.example/watch/one",
    )

    first = media_bin_add(channel, project_id, body, request)
    replay = media_bin_add(channel, project_id, body, request)
    assert replay == first
    assert first.status == "reference_only"
    assert first.rights_status == "reference_only"
    assert first.requirement_ids == ["visual-1"]
    assert len(media_bin_list(channel, project_id, request, revision=None)) == 1
    assert media_bin_list(channel, project_id, request, revision=revision)[0].id == first.id
    assert media_bin_detail(channel, project_id, first.id, request) == first


def test_api_checks_channel_and_write_scope_and_rejects_signed_urls(media_bin_api):
    channel, other_channel, project_id, revision = media_bin_api
    request = authorized_request(channel)
    with pytest.raises(HTTPException) as signed_url:
        media_bin_add(
            channel,
            project_id,
            add_body(revision, "https://video.example/watch/one?token=temporary"),
            request,
        )
    assert signed_url.value.status_code == 422

    with pytest.raises(HTTPException) as wrong_channel:
        media_bin_list(other_channel, project_id, request)
    assert wrong_channel.value.status_code == 403
    media_bin_add(
        channel,
        project_id,
        add_body(revision, "https://video.example/watch/one"),
        request,
    )
    reader = authorized_request(channel, scopes={"ai:read"})
    assert len(media_bin_list(channel, project_id, reader, revision=None)) == 1
    with pytest.raises(HTTPException) as denied_write:
        media_bin_add(
            channel,
            project_id,
            add_body(revision, "https://video.example/watch/one"),
            reader,
        )
    assert denied_write.value.status_code == 403


def test_existing_clip_must_be_available_to_the_requested_channel(media_bin_api):
    channel, other_channel, project_id, revision = media_bin_api
    clip_id = uuid.uuid4()
    with db.session_scope() as session:
        session.add(
            Clip(
                id=clip_id,
                sha256=uuid.uuid4().hex * 2,
                storage_key="canonical/other-channel.mp4",
                status="scored",
            )
        )
        session.add(
            SourceItem(
                source_url="https://owned.example/other-channel.mp4",
                canonical_url="https://owned.example/other-channel.mp4",
                platform="web",
                clip_id=clip_id,
                source_metadata={"channel_profile_id": str(other_channel)},
            )
        )
    body = AddProjectMediaItemRequest.model_validate(
        {"revision": revision, "kind": "video", "clip_id": str(clip_id)}
    )
    with pytest.raises(HTTPException) as denied_clip:
        media_bin_add(channel, project_id, body, authorized_request(channel))
    assert denied_clip.value.status_code == 409


def test_unknown_project_revision_and_item_return_not_found(media_bin_api):
    channel, _, project_id, _ = media_bin_api
    request = authorized_request(channel)
    with pytest.raises(HTTPException) as unknown_revision:
        media_bin_list(channel, project_id, request, revision=99)
    assert unknown_revision.value.status_code == 404
    with pytest.raises(HTTPException) as unknown_item:
        media_bin_detail(channel, project_id, uuid.uuid4(), request)
    assert unknown_item.value.status_code == 404

def test_media_bin_export_requires_channel_scope_and_returns_download(
    monkeypatch, media_bin_api, tmp_path
):
    channel, other_channel, project_id, revision = media_bin_api
    archive_path = tmp_path / "project.zip"
    archive_path.write_bytes(b"portable zip bytes")
    monkeypatch.setattr(
        "katcha.editorial.media_intake.export.build_project_media_export_file",
        lambda *args: archive_path,
    )
    response = media_bin_export(
        channel,
        project_id,
        authorized_request(channel, scopes={"ai:read"}),
        revision,
    )
    assert response.path == archive_path
    assert not hasattr(response, "body")
    assert response.path.read_bytes() == b"portable zip bytes"
    assert response.media_type == "application/zip"
    assert response.headers["cache-control"] == "no-store"
    with pytest.raises(HTTPException) as wrong_channel:
        media_bin_export(
            other_channel,
            project_id,
            authorized_request(channel, scopes={"ai:read"}),
            revision,
        )
    assert wrong_channel.value.status_code == 403
