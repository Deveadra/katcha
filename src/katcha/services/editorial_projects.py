"""Transactional draft storage. No provider call or publication side effects."""

from __future__ import annotations

import hashlib
import json
import uuid

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from katcha.db import session_scope
from katcha.editorial.project_schemas import (
    CreateEditorialProject,
    EditorialBrief,
    SaveEditorialDraft,
)
from katcha.editorial_models import EditorialProject, EditorialRevision
from katcha.models import Clip, DomainEvent, SourceItem
from katcha.services.channel_profiles import ensure_active_profile
from katcha.services.clip_lifecycle import channel_ids_for_clip


class EditorialConflict(ValueError):
    pass


class EditorialNotFound(ValueError):
    pass


def _digest(value: dict) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def validate_source_clip_binding(
    session: Session,
    channel_id: uuid.UUID,
    source_url: str,
    clip_id: uuid.UUID,
) -> None:
    if session.get(Clip, clip_id) is None:
        raise EditorialNotFound("Selected source clip not found")
    if channel_id not in channel_ids_for_clip(session, clip_id):
        raise EditorialConflict("Selected source clip is not available to this channel")
    lineage = session.scalar(
        select(SourceItem.id).where(
            SourceItem.clip_id == clip_id,
            or_(
                SourceItem.source_url == source_url,
                SourceItem.canonical_url == source_url,
            ),
        )
    )
    if lineage is None:
        raise EditorialConflict(
            "Selected source clip does not match the recorded source URL"
        )


def _validate_source_clip_bindings(
    session: Session,
    channel_id: uuid.UUID,
    brief: EditorialBrief,
) -> None:
    for source_url, clip_id in brief.source_clip_bindings.items():
        validate_source_clip_binding(session, channel_id, source_url, clip_id)


def get_project(channel_id: uuid.UUID, project_id: uuid.UUID) -> EditorialProject:
    with session_scope() as session:
        row = session.get(EditorialProject, project_id)
        if row is None or row.channel_profile_id != channel_id:
            raise EditorialNotFound("Editorial project not found in this channel")
        session.expunge(row)
        return row


def create_project(
    channel_id: uuid.UUID, request: CreateEditorialProject, *, actor: str
) -> EditorialProject:
    project_id = uuid.uuid5(channel_id, f"editorial-project:{request.idempotency_key}")
    brief = request.brief.model_dump(mode="json")
    digest = _digest(brief)
    try:
        with session_scope() as session:
            ensure_active_profile(session, channel_id)
            _validate_source_clip_bindings(session, channel_id, request.brief)
            row = session.get(EditorialProject, project_id)
            if row is not None:
                if row.input_digest != digest:
                    raise EditorialConflict(
                        "Request identity was already used for a different brief"
                    )
                session.expunge(row)
                return row
            row = EditorialProject(
                id=project_id, channel_profile_id=channel_id, brief=brief, input_digest=digest
            )
            session.add(row)
            session.add(
                DomainEvent(
                    aggregate_type="editorial_project",
                    aggregate_id=str(project_id),
                    event_type="editorial.project_created",
                    payload={"channel_profile_id": str(channel_id), "actor": actor, "revision": 0},
                )
            )
            session.flush()
            session.refresh(row)
            session.expunge(row)
            return row
    except IntegrityError as exc:
        # A simultaneous identical request may have won the unique primary key.
        try:
            row = get_project(channel_id, project_id)
        except EditorialNotFound:
            raise exc from None
        if row.input_digest != digest:
            raise EditorialConflict(
                "Request identity was already used for a different brief"
            ) from None
        return row


def save_draft(
    channel_id: uuid.UUID, project_id: uuid.UUID, request: SaveEditorialDraft, *, actor: str
) -> EditorialRevision:
    with session_scope() as session:
        return save_draft_in_session(session, channel_id, project_id, request, actor=actor)


def save_draft_in_session(
    session: Session,
    channel_id: uuid.UUID,
    project_id: uuid.UUID,
    request: SaveEditorialDraft,
    *,
    actor: str,
) -> EditorialRevision:
    """Shared transaction boundary for draft storage and workflow completion."""
    draft = request.draft.model_dump(mode="json")
    digest = _digest(draft)
    request_digest = _digest(request.model_dump(mode="json"))
    request_id = uuid.uuid5(project_id, f"editorial-save:{request.idempotency_key}")
    ensure_active_profile(session, channel_id)
    project = session.get(EditorialProject, project_id)
    if project is None or project.channel_profile_id != channel_id:
        raise EditorialNotFound("Editorial project not found in this channel")
    observed_urls = {str(item.source_url) for item in request.draft.observations}
    if not observed_urls <= set(project.brief["source_urls"]):
        raise EditorialConflict("Observations must reference a source URL in the project brief")
    # Acquire the project row before checking replay, including concurrent retries.
    session.execute(
        update(EditorialProject)
        .where(EditorialProject.id == project_id)
        .values(revision=EditorialProject.revision, updated_at=EditorialProject.updated_at)
    )
    previous = session.scalar(
        select(EditorialRevision).where(EditorialRevision.request_id == request_id)
    )
    if previous is not None:
        if previous.request_digest != request_digest:
            raise EditorialConflict("Save identity was already used for a different revision")
        session.expunge(previous)
        return previous
    result = session.execute(
        update(EditorialProject)
        .where(
            EditorialProject.id == project_id,
            EditorialProject.revision == request.expected_revision,
        )
        .values(revision=request.expected_revision + 1)
    )
    if result.rowcount != 1:
        raise EditorialConflict("Draft changed; reload the current revision before saving")
    row = EditorialRevision(
        project_id=project_id,
        revision=request.expected_revision + 1,
        request_id=request_id,
        request_digest=request_digest,
        digest=digest,
        draft=draft,
        actor=actor,
    )
    session.add(row)
    session.add(
        DomainEvent(
            aggregate_type="editorial_project",
            aggregate_id=str(project_id),
            event_type="editorial.draft_saved",
            payload={
                "channel_profile_id": str(channel_id),
                "revision": row.revision,
                "digest": digest,
                "actor": actor,
            },
        )
    )
    session.flush()
    session.refresh(row)
    session.expunge(row)
    return row
