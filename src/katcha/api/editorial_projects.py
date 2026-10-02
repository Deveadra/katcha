from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import select

from katcha.api.control_auth import control_actor, require_control_channel, require_control_scope
from katcha.db import session_scope
from katcha.editorial.project_schemas import (
    CreateEditorialProject,
    EditorialProjectResponse,
    EditorialRevisionResponse,
    SaveEditorialDraft,
)
from katcha.editorial_models import EditorialProject, EditorialRevision
from katcha.services.editorial_projects import (
    EditorialConflict,
    EditorialNotFound,
    create_project,
    get_project,
    save_draft,
)

router = APIRouter(
    prefix="/v1/channels/{channel_profile_id}/editorial-projects", tags=["editorial-projects"]
)


def _authorize(request: Request, channel_id: uuid.UUID, *, write: bool = False) -> None:
    require_control_channel(request, channel_id)
    require_control_scope(request, "production:create" if write else "ai:read")


def _project(row: EditorialProject) -> EditorialProjectResponse:
    return EditorialProjectResponse(
        id=str(row.id),
        channel_profile_id=str(row.channel_profile_id),
        brief=row.brief,
        revision=row.revision,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _revision(row: EditorialRevision) -> EditorialRevisionResponse:
    return EditorialRevisionResponse(
        revision=row.revision,
        digest=row.digest,
        draft=row.draft,
        created_at=row.created_at,
        actor=row.actor,
    )


def _error(exc: ValueError) -> HTTPException:
    if isinstance(exc, EditorialNotFound):
        return HTTPException(404, str(exc))
    return HTTPException(409 if isinstance(exc, EditorialConflict) else 422, str(exc))


@router.post("", response_model=EditorialProjectResponse, status_code=201)
def create(
    channel_profile_id: uuid.UUID, body: CreateEditorialProject, request: Request
) -> EditorialProjectResponse:
    _authorize(request, channel_profile_id, write=True)
    try:
        return _project(create_project(channel_profile_id, body, actor=control_actor(request)))
    except ValueError as exc:
        raise _error(exc) from exc


@router.get("", response_model=list[EditorialProjectResponse])
def list_projects(
    channel_profile_id: uuid.UUID,
    request: Request,
    offset: int = Query(0, ge=0, le=10000),
    limit: int = Query(20, ge=1, le=100),
) -> list[EditorialProjectResponse]:
    _authorize(request, channel_profile_id)
    with session_scope() as session:
        rows = session.scalars(
            select(EditorialProject)
            .where(EditorialProject.channel_profile_id == channel_profile_id)
            .order_by(EditorialProject.created_at.desc(), EditorialProject.id)
            .offset(offset)
            .limit(limit)
        )
        return [_project(row) for row in rows]


@router.get("/{project_id}", response_model=EditorialProjectResponse)
def detail(
    channel_profile_id: uuid.UUID, project_id: uuid.UUID, request: Request
) -> EditorialProjectResponse:
    _authorize(request, channel_profile_id)
    try:
        return _project(get_project(channel_profile_id, project_id))
    except ValueError as exc:
        raise _error(exc) from exc


@router.post("/{project_id}/revisions", response_model=EditorialRevisionResponse, status_code=201)
def save(
    channel_profile_id: uuid.UUID, project_id: uuid.UUID, body: SaveEditorialDraft, request: Request
) -> EditorialRevisionResponse:
    _authorize(request, channel_profile_id, write=True)
    try:
        return _revision(
            save_draft(channel_profile_id, project_id, body, actor=control_actor(request))
        )
    except ValueError as exc:
        raise _error(exc) from exc


@router.get("/{project_id}/revisions", response_model=list[EditorialRevisionResponse])
def history(
    channel_profile_id: uuid.UUID,
    project_id: uuid.UUID,
    request: Request,
    offset: int = Query(0, ge=0, le=10000),
    limit: int = Query(10, ge=1, le=20),
) -> list[EditorialRevisionResponse]:
    _authorize(request, channel_profile_id)
    try:
        get_project(channel_profile_id, project_id)
    except ValueError as exc:
        raise _error(exc) from exc
    with session_scope() as session:
        rows = session.scalars(
            select(EditorialRevision)
            .where(EditorialRevision.project_id == project_id)
            .order_by(EditorialRevision.revision.desc())
            .offset(offset)
            .limit(limit)
        )
        return [_revision(row) for row in rows]
