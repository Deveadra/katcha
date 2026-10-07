"""Review decisions use synthetic render receipts; no publication side effects."""

import importlib.util
import uuid
from datetime import UTC, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import Mock

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import sessionmaker
from test_editorial_projects import saved as _saved
from test_editorial_render import setup_render

from katcha import db
from katcha.editorial import render
from katcha.editorial.review_schemas import ReviewEditorialRender
from katcha.editorial_models import EditorialRenderReview, EditorialRun
from katcha.intelligence_models import ChannelProfile
from katcha.models import DomainEvent
from katcha.orchestration import publishing_activities
from katcha.publishing_models import Publication, YouTubeConnection
from katcha.services import editorial_reviews as reviews
from katcha.services.packaging_generation import compile_packaging_context
from katcha.services.editorial_projects import EditorialConflict

saved = _saved


def completed(saved, monkeypatch):
    row, manifest, _, _, _ = setup_render(saved, monkeypatch)
    with db.session_scope() as session:
        stored = session.get(EditorialRun, row.id)
        stored.options = {**stored.options, "target": "render"}
    assert render.render_project(str(row.id), 1)["status"] == "completed"
    monkeypatch.setattr(reviews, "current_manifest", Mock(return_value=manifest))
    return row


def request(**values):
    return ReviewEditorialRender(
        **{"idempotency_key": "review", "expected_revision": 1,
           "expected_review_sequence": 0, "decision": "approve", **values}
    )


def save(row, **values):
    return reviews.review_render(
        row.channel_profile_id, row.project_id, row.id,
        request(**values), actor="reviewer",
    )


def status(row):
    return reviews.review_status(row.channel_profile_id, row.project_id, row.id)


def test_review_replay_reversal_and_history(saved, monkeypatch):
    row = completed(saved, monkeypatch)
    # The generic render fixture begins at revision zero; use a saved revision identity.
    with db.session_scope() as session:
        session.get(EditorialRun, row.id).input_revision = 1
    assert status(row)["status"] == "unreviewed"
    first = save(row, note="Evidence and timing checked")
    assert save(row, note="Evidence and timing checked") == first
    assert status(row)["status"] == "approve"
    assert status(row)["publication_available"] is True
    with pytest.raises(EditorialConflict, match="identity"):
        save(row, decision="request_changes")
    with pytest.raises(EditorialConflict, match="Another review"):
        save(row, idempotency_key="second")
    save(row, idempotency_key="second", expected_review_sequence=1, decision="request_changes")
    assert status(row)["status"] == "request_changes"
    with db.session_scope() as session:
        assert len(list(session.scalars(select(EditorialRenderReview)))) == 2
        events = list(session.scalars(select(DomainEvent).where(
            DomainEvent.event_type == "editorial.render_reviewed"
        )))
        assert len(events) == 2


def test_approved_editorial_render_stages_publication_and_rechecks_clearance(
    saved, monkeypatch
):
    row = completed(saved, monkeypatch)
    with db.session_scope() as session:
        session.get(EditorialRun, row.id).input_revision = 1
        profile = session.get(ChannelProfile, row.channel_profile_id)
        connection_id = profile.youtube_connection_id
        session.add(
            YouTubeConnection(
                id=connection_id,
                channel_id="UC" + "e" * 22,
                channel_title="Editorial Test",
                status="active",
                scopes=["https://www.googleapis.com/auth/youtube"],
                encrypted_access_token="encrypted",
                encrypted_refresh_token="encrypted",
                token_expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
    save(row, note="Approved for packaging")
    monkeypatch.setattr("katcha.api.main._require_youtube_execution", lambda: None)

    client = saved[0]
    url = (
        f"/v1/channels/{row.channel_profile_id}/editorial-projects/{row.project_id}"
        f"/runs/{row.id}/publication"
    )
    body = {
        "youtube_connection_id": str(connection_id),
        "title": "Exact approved editorial render",
    }
    response = client.post(url, json=body)
    assert response.status_code == 201, response.text
    publication = response.json()
    assert publication["editorial_run_id"] == str(row.id)
    assert publication["production_id"] is None
    assert publication["compilation_id"] is None
    assert publication["short_episode_id"] is None
    assert publication["stage"] == "metadata_hold"
    assert publication["raw_status"]["source_kind"] == "editorial_render"
    assert publication["treatment_metadata"]["render_approval_sequence"] == 1

    replay = client.post(url, json=body)
    assert replay.status_code == 201
    assert replay.json()["id"] == publication["id"]

    profile_id, context, context_digest = compile_packaging_context(
        uuid.UUID(publication["id"])
    )
    assert profile_id == row.channel_profile_id
    assert context["source_kind"] == "editorial_render"
    assert context["source_id"] == str(row.id)
    assert context["render_approval_sequence"] == 1
    assert "This may be a connection." in context["grounding_facts"]
    assert len(context_digest) == 64

    reviews.current_manifest.side_effect = EditorialConflict("clearance revoked")
    assert client.post(url, json=body).status_code == 409
    assert client.post(
        f"/v1/publications/{publication['id']}/start",
        json={},
    ).status_code == 409
    with db.session_scope() as session:
        stored = session.get(Publication, uuid.UUID(publication["id"]))
        with pytest.raises(RuntimeError, match="clearance revoked"):
            publishing_activities._publication_render_key(session, stored)
        stored.status = "processing"
        stored.stage = "processing"
        stored.youtube_video_id = "private-video-id"
        stored.privacy_status = "public"

    youtube = Mock()
    monkeypatch.setattr(publishing_activities, "YouTubeClient", youtube)
    with pytest.raises(RuntimeError, match="clearance revoked"):
        publishing_activities.finalize_publication_activity(publication["id"])
    youtube.assert_not_called()


@pytest.mark.parametrize("damage", ["rights", "receipt", "duration", "composition"])
def test_approval_invalidates_and_cannot_be_reused(saved, monkeypatch, damage):
    row = completed(saved, monkeypatch)
    with db.session_scope() as session:
        session.get(EditorialRun, row.id).input_revision = 1
    original = save(row)
    if damage == "rights":
        reviews.current_manifest.side_effect = EditorialConflict("clearance revoked")
    else:
        with db.session_scope() as session:
            stored = session.get(EditorialRun, row.id)
            result = dict(stored.artifacts["render_result"])
            if damage == "receipt":
                result["metadata"] = {**result["metadata"], "changed": True}
            elif damage == "duration":
                result["duration_seconds"] = True
            else:
                result["metadata"] = {**result["metadata"], "composition": "Short"}
            stored.artifacts = {**stored.artifacts, "render_result": result}
    assert status(row)["status"] == "invalidated"
    assert save(row) == original  # Historical replay does not reinstate approval.
    assert status(row)["status"] == "invalidated"
    if damage != "receipt":
        with pytest.raises(EditorialConflict):
            save(row, idempotency_key="new", expected_review_sequence=1)
    save(row, idempotency_key="reject", expected_review_sequence=1, decision="request_changes")
    assert status(row)["status"] == "request_changes"


def test_api_scopes_channel_isolation_and_actor(saved, monkeypatch):
    row = completed(saved, monkeypatch)
    with db.session_scope() as session:
        session.get(EditorialRun, row.id).input_revision = 1
    client, channel, other = saved
    url = f"/v1/channels/{channel}/editorial-projects/{row.project_id}/runs/{row.id}/review"
    body = request().model_dump(mode="json")
    assert client.get(url).json()["sequence"] == 0
    assert client.post(url, json=body, headers={
        "Authorization": "Bearer editorial-reader-token-00001"
    }).status_code == 403
    assert client.post(url.replace(str(channel), str(other)), json=body).status_code == 403
    assert client.post(url.replace(str(row.id), str(uuid.uuid4())), json=body).status_code == 404
    response = client.post(url, json=body)
    assert response.status_code == 201, response.text
    assert response.json()["actor"] == "control-principal:editor"
    assert client.get(url).json()["status"] == "approve"


@pytest.mark.parametrize("same_request", [False, True])
def test_concurrent_reviews_serialize(saved, monkeypatch, tmp_path, same_request):
    row = completed(saved, monkeypatch)
    # Copy this fixture to a file DB so each thread owns a separate real connection.
    original_engine = db.SessionLocal.kw["bind"]
    engine = create_engine(f"sqlite:///{tmp_path / 'reviews.db'}")
    db.Base.metadata.create_all(engine)
    with original_engine.connect() as source, engine.begin() as target:
        for table in db.Base.metadata.sorted_tables:
            rows = [dict(item) for item in source.execute(select(table)).mappings()]
            if rows:
                target.execute(table.insert(), rows)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=engine, expire_on_commit=False))
    with db.session_scope() as session:
        session.get(EditorialRun, row.id).input_revision = 1
    barrier = Barrier(2)

    def write(index):
        barrier.wait(timeout=5)
        try:
            return save(row, idempotency_key="same" if same_request else str(index))["sequence"]
        except EditorialConflict:
            return "conflict"

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(write, [1, 2]))
        assert results == [1, 1] if same_request else set(results) == {1, "conflict"}
        assert status(row)["sequence"] == 1
    finally:
        engine.dispose()


def test_review_migration_roundtrip():
    spec = importlib.util.spec_from_file_location(
        "review_migration", "migrations/versions/0052_editorial_render_reviews.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        module.op = Operations(MigrationContext.configure(connection))
        module.upgrade()
        assert "editorial_render_reviews" in inspect(connection).get_table_names()
        module.downgrade()
        assert "editorial_render_reviews" not in inspect(connection).get_table_names()
    engine.dispose()
