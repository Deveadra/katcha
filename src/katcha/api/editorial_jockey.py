"""Explicitly authorized Jockey indexing and semantic review endpoints."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Request

from katcha.api.control_auth import control_actor
from katcha.api.editorial_projects import _authorize, _error
from katcha.editorial.jockey_schemas import JockeyAdmission, JockeySearchRequest
from katcha.services.editorial_jockey import (
    advance_index,
    get_index,
    get_search,
    search_jockey,
    start_index,
)

router = APIRouter(
    prefix="/v1/channels/{channel_profile_id}/editorial-projects/{project_id}/jockey",
    tags=["editorial-projects"],
)


@router.post("/media/{item_id}/index")
def index_media(
    channel_profile_id: uuid.UUID,
    project_id: uuid.UUID,
    item_id: uuid.UUID,
    body: JockeyAdmission,
    request: Request,
) -> dict[str, Any]:
    _authorize(request, channel_profile_id, write=True)
    try:
        return start_index(
            channel_profile_id, project_id, item_id, body, actor=control_actor(request)
        )
    except ValueError as exc:
        raise _error(exc) from exc


@router.post("/media/{item_id}/advance")
def advance_media(
    channel_profile_id: uuid.UUID,
    project_id: uuid.UUID,
    item_id: uuid.UUID,
    body: JockeyAdmission,
    request: Request,
) -> dict[str, Any]:
    _authorize(request, channel_profile_id, write=True)
    try:
        return advance_index(channel_profile_id, project_id, item_id, body)
    except ValueError as exc:
        raise _error(exc) from exc


@router.get("/media/{item_id}")
def read_media(
    channel_profile_id: uuid.UUID,
    project_id: uuid.UUID,
    item_id: uuid.UUID,
    request: Request,
) -> dict[str, Any]:
    _authorize(request, channel_profile_id)
    try:
        return get_index(channel_profile_id, project_id, item_id)
    except ValueError as exc:
        raise _error(exc) from exc


@router.post("/search")
def search(
    channel_profile_id: uuid.UUID,
    project_id: uuid.UUID,
    body: JockeySearchRequest,
    request: Request,
) -> dict[str, Any]:
    _authorize(request, channel_profile_id, write=True)
    try:
        return search_jockey(
            channel_profile_id, project_id, body, actor=control_actor(request)
        )
    except ValueError as exc:
        raise _error(exc) from exc


@router.get("/search/{run_id}")
def read_search(
    channel_profile_id: uuid.UUID,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    request: Request,
) -> dict[str, Any]:
    _authorize(request, channel_profile_id)
    try:
        return get_search(channel_profile_id, project_id, run_id)
    except ValueError as exc:
        raise _error(exc) from exc
