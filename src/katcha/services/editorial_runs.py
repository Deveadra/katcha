"""Durable execution intent, checkpoints and attempt-fenced state transitions."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from urllib.parse import urlsplit

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from katcha.db import session_scope
from katcha.editorial.run_schemas import StartEditorialRun
from katcha.editorial_models import EditorialProject, EditorialRun
from katcha.models import Clip, DomainEvent
from katcha.services.channel_profiles import ensure_active_profile
from katcha.services.clip_lifecycle import channel_ids_for_clip
from katcha.services.editorial_projects import EditorialConflict, EditorialNotFound, _digest

ACTIVE = ("queued", "running")


class EditorialStopped(RuntimeError):
    pass


def workflow_id(row: EditorialRun) -> str:
    return f"editorial-{row.id}-{row.attempt}"


def _event(session: Session, row: EditorialRun, name: str) -> None:
    session.add(
        DomainEvent(
            aggregate_type="editorial_project",
            aggregate_id=str(row.project_id),
            event_type=f"editorial.{name}",
            payload={
                "channel_profile_id": str(row.channel_profile_id),
                "project_id": str(row.project_id),
                "editorial_run_id": str(row.id),
                "attempt": row.attempt,
                "stage": row.stage,
                "status": row.status,
                "actor": row.actor,
            },
        )
    )


def get_run(channel_id: uuid.UUID, project_id: uuid.UUID, run_id: uuid.UUID) -> EditorialRun:
    with session_scope() as session:
        row = session.get(EditorialRun, run_id)
        if row is None or (row.channel_profile_id, row.project_id) != (channel_id, project_id):
            raise EditorialNotFound("Editorial run not found in this project")
        session.expunge(row)
        return row


def start_run(
    channel_id: uuid.UUID, project_id: uuid.UUID, request: StartEditorialRun, *, actor: str
) -> EditorialRun:
    run_id = uuid.uuid5(project_id, f"editorial-run:{request.idempotency_key}")
    options = request.model_dump(mode="json")
    digest = _digest(options)
    with session_scope() as session:
        ensure_active_profile(session, channel_id)
        project = session.scalar(
            select(EditorialProject)
            .where(
                EditorialProject.id == project_id,
                EditorialProject.channel_profile_id == channel_id,
            )
            .with_for_update()
        )
        if project is None:
            raise EditorialNotFound("Editorial project not found in this channel")
        # Serialize start requests on SQLite as well as PostgreSQL.
        session.execute(
            update(EditorialProject)
            .where(EditorialProject.id == project_id)
            .values(
                revision=EditorialProject.revision,
                updated_at=EditorialProject.updated_at,
            )
        )
        previous = session.get(EditorialRun, run_id)
        if previous is not None:
            if previous.input_digest != digest:
                raise EditorialConflict("Run identity was already used for different options")
            session.expunge(previous)
            return previous
        session.refresh(project)
        if project.revision != request.expected_revision:
            raise EditorialConflict("Draft changed; reload before starting work")
        active = session.scalar(
            select(EditorialRun.id).where(
                EditorialRun.project_id == project_id, EditorialRun.status.in_(ACTIVE)
            )
        )
        if active is not None:
            raise EditorialConflict("This project already has active work; inspect or cancel it")
        urls = project.brief["source_urls"]
        if not set(request.clip_bindings) <= set(urls):
            raise EditorialConflict("Clip bindings must match source URLs in this brief")
        for url in urls:
            clip_id = request.clip_bindings.get(url)
            if clip_id:
                if session.get(Clip, clip_id) is None:
                    raise EditorialNotFound("Selected source clip not found")
                if channel_id not in channel_ids_for_clip(session, clip_id):
                    raise EditorialConflict("Selected source clip is not available to this channel")
            else:
                parsed = urlsplit(url)
                if (
                    parsed.scheme != "https"
                    or parsed.username
                    or parsed.password
                    or parsed.port not in {None, 443}
                    or parsed.hostname
                    not in {
                        "youtube.com",
                        "www.youtube.com",
                        "m.youtube.com",
                        "youtu.be",
                    }
                    or not (
                        parsed.path == "/watch"
                        or parsed.hostname == "youtu.be"
                        or parsed.path.startswith(("/shorts/", "/embed/"))
                    )
                ):
                    raise EditorialConflict(
                        "Automatic editorial intake currently accepts HTTPS YouTube videos. "
                        "For other sources, ingest/upload first and select a channel clip."
                    )
        row = EditorialRun(
            id=run_id,
            project_id=project_id,
            channel_profile_id=channel_id,
            input_digest=digest,
            input_revision=project.revision,
            options=options,
            status="queued",
            stage="intake",
            attempt=1,
            actor=actor,
            artifacts={"brief": project.brief},
        )
        session.add(row)
        _event(session, row, "run_queued")
        session.flush()
        session.refresh(row)
        session.expunge(row)
        return row


def checkpoint(
    run_id: str,
    attempt: int,
    *,
    stage: str | None = None,
    artifacts: dict | None = None,
    status: str = "running",
    error: str | None = None,
) -> EditorialRun:
    with session_scope() as session:
        row = session.scalar(
            select(EditorialRun).where(EditorialRun.id == uuid.UUID(run_id)).with_for_update()
        )
        if row is None or row.attempt != attempt or row.status not in ACTIVE:
            raise EditorialStopped("Editorial attempt was cancelled, superseded or completed")
        values = {
            "stage": stage or row.stage,
            "status": status,
            "error": error,
            "artifacts": {**row.artifacts, **(artifacts or {})},
            "updated_at": datetime.now(UTC),
        }
        updated = session.execute(
            update(EditorialRun)
            .where(
                EditorialRun.id == row.id,
                EditorialRun.attempt == attempt,
                EditorialRun.status.in_(ACTIVE),
            )
            .values(**values)
        )
        if updated.rowcount != 1:
            raise EditorialStopped("Editorial attempt changed while saving")
        session.refresh(row)
        _event(session, row, "run_progress")
        session.flush()
        session.expunge(row)
        return row


def control_run(
    channel_id: uuid.UUID,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    *,
    expected_attempt: int,
    cancel: bool,
) -> EditorialRun:
    with session_scope() as session:
        # Share start_run's project lock so a resume and a new start cannot both win.
        project_lock = session.execute(
            update(EditorialProject)
            .where(
                EditorialProject.id == project_id,
                EditorialProject.channel_profile_id == channel_id,
            )
            .values(revision=EditorialProject.revision, updated_at=EditorialProject.updated_at)
        )
        if project_lock.rowcount != 1:
            raise EditorialNotFound("Editorial project not found in this channel")
        row = session.scalar(
            select(EditorialRun)
            .where(
                EditorialRun.id == run_id,
                EditorialRun.project_id == project_id,
                EditorialRun.channel_profile_id == channel_id,
            )
            .with_for_update()
        )
        if row is None:
            raise EditorialNotFound("Editorial run not found in this project")
        # Idempotent retry after an uncertain resume/cancel response.
        if not cancel and row.attempt == expected_attempt + 1:
            session.expunge(row)
            return row
        if row.attempt != expected_attempt:
            raise EditorialConflict("Run attempt changed; reload its current status")
        if cancel:
            if row.status in {"completed", "cancelled"}:
                session.expunge(row)
                return row
            values = {"status": "cancelled"}
        else:
            ensure_active_profile(session, channel_id)
            if row.status not in {"blocked", "failed"}:
                raise EditorialConflict("Only blocked or failed work can be resumed")
            project = session.get(EditorialProject, project_id)
            if project.revision != row.input_revision:
                raise EditorialConflict("Draft changed; start a new run for the current revision")
            active = session.scalar(
                select(EditorialRun.id).where(
                    EditorialRun.project_id == project_id, EditorialRun.status.in_(ACTIVE)
                )
            )
            if active is not None:
                raise EditorialConflict("Another run is active for this project")
            values = {"status": "queued", "attempt": expected_attempt + 1, "error": None}
        result = session.execute(
            update(EditorialRun)
            .where(
                EditorialRun.id == run_id,
                EditorialRun.attempt == expected_attempt,
                EditorialRun.status == row.status,
            )
            .values(**values)
        )
        if result.rowcount != 1:
            raise EditorialConflict("Run changed while saving; reload its current status")
        session.refresh(row)
        _event(session, row, "run_cancelled" if cancel else "run_resumed")
        session.flush()
        session.expunge(row)
        return row
