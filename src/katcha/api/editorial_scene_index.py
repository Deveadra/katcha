"""Scoped measured index/search endpoints alongside the existing Project Media Bin."""

import uuid
from typing import Any

from fastapi import APIRouter, Request

from katcha.api.control_auth import control_actor
from katcha.api.editorial_projects import _authorize, _error
from katcha.editorial.media_intake.scene_index import (
    cancel_project_scene_index,
    get_project_scene_index,
    index_project_media,
    reconcile_project_scene_index,
    search_project_shots,
)
from katcha.editorial.media_intake.scene_schemas import (
    IndexMediaRequest,
    ReconcileIndexRequest,
    SearchShotsRequest,
)

router = APIRouter(
    prefix="/v1/channels/{channel_profile_id}/editorial-projects/{project_id}/media-bin",
    tags=["editorial-projects"],
)


@router.post("/{item_id}/scene-index", status_code=202)
def index_media(
    channel_profile_id: uuid.UUID,
    project_id: uuid.UUID,
    item_id: uuid.UUID,
    body: IndexMediaRequest,
    request: Request,
) -> dict[str, Any]:
    _authorize(request, channel_profile_id, write=True)
    try:
        return index_project_media(
            channel_profile_id,
            project_id,
            item_id,
            actor=control_actor(request),
            retry_failed=body.retry_failed,
            enqueue=True,
        )
    except ValueError as exc:
        raise _error(exc) from exc


@router.get("/{item_id}/scene-index")
def scene_index(
    channel_profile_id: uuid.UUID,
    project_id: uuid.UUID,
    item_id: uuid.UUID,
    request: Request,
) -> dict[str, Any]:
    _authorize(request, channel_profile_id)
    try:
        return get_project_scene_index(channel_profile_id, project_id, item_id)
    except ValueError as exc:
        raise _error(exc) from exc


@router.post("/shot-search")
def shot_search(
    channel_profile_id: uuid.UUID,
    project_id: uuid.UUID,
    body: SearchShotsRequest,
    request: Request,
) -> list[dict[str, Any]]:
    _authorize(request, channel_profile_id)
    try:
        return search_project_shots(channel_profile_id, project_id, body)
    except ValueError as exc:
        raise _error(exc) from exc


@router.post("/{item_id}/scene-index/reconcile")
def reconcile_index(
    channel_profile_id: uuid.UUID,
    project_id: uuid.UUID,
    item_id: uuid.UUID,
    body: ReconcileIndexRequest,
    request: Request,
) -> dict[str, Any]:
    _authorize(request, channel_profile_id, write=True)
    try:
        return reconcile_project_scene_index(
            channel_profile_id,
            project_id,
            item_id,
            confirmed_worker_stopped=body.confirmed_worker_stopped,
            actor=control_actor(request),
        )
    except ValueError as exc:
        raise _error(exc) from exc


@router.post("/{item_id}/scene-index/cancel")
def cancel_index(
    channel_profile_id: uuid.UUID, project_id: uuid.UUID, item_id: uuid.UUID, request: Request
) -> dict[str, Any]:
    _authorize(request, channel_profile_id, write=True)
    try:
        return cancel_project_scene_index(
            channel_profile_id, project_id, item_id, actor=control_actor(request)
        )
    except ValueError as exc:
        raise _error(exc) from exc
