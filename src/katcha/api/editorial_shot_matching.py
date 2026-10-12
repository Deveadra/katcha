"""Backend requirement matching and explicit operator choices; no indexing UI."""

import uuid
from typing import Any

from fastapi import APIRouter, Request

from katcha.api.control_auth import control_actor
from katcha.api.editorial_projects import _authorize, _error
from katcha.editorial.source_intelligence.shot_contracts import (
    MatchRequirementsRequest,
    SelectShotRequest,
)
from katcha.services.editorial_shot_matching import (
    get_shot_package,
    match_requirements,
    select_shot,
)

router = APIRouter(
    prefix="/v1/channels/{channel_profile_id}/editorial-projects/{project_id}/shot-proposals",
    tags=["editorial-projects"],
)


@router.post("")
def propose(
    channel_profile_id: uuid.UUID,
    project_id: uuid.UUID,
    body: MatchRequirementsRequest,
    request: Request,
) -> dict[str, Any]:
    _authorize(request, channel_profile_id, write=True)
    try:
        return match_requirements(
            channel_profile_id, project_id, body, actor=control_actor(request)
        )
    except ValueError as exc:
        raise _error(exc) from exc


@router.get("/{package_id}")
def read(
    channel_profile_id: uuid.UUID, project_id: uuid.UUID, package_id: uuid.UUID, request: Request
) -> dict[str, Any]:
    _authorize(request, channel_profile_id)
    try:
        return get_shot_package(channel_profile_id, project_id, package_id)
    except ValueError as exc:
        raise _error(exc) from exc


@router.post("/choices")
def choose(
    channel_profile_id: uuid.UUID, project_id: uuid.UUID, body: SelectShotRequest, request: Request
) -> dict[str, Any]:
    _authorize(request, channel_profile_id, write=True)
    try:
        return select_shot(channel_profile_id, project_id, body, actor=control_actor(request))
    except ValueError as exc:
        raise _error(exc) from exc
