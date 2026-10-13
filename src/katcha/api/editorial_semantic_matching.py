"""Backend opt-in inspection; existing channel auth applies to source transmission."""

import uuid

from fastapi import APIRouter, HTTPException, Request

from katcha.api.control_auth import control_actor
from katcha.api.editorial_projects import _authorize, _error
from katcha.editorial.provider import EditorialBlocked
from katcha.editorial.semantic_schemas import SemanticMatchRequest
from katcha.services.editorial_semantic_matching import get_semantic_match, match_semantics

router = APIRouter(
    prefix="/v1/channels/{channel_profile_id}/editorial-projects/{project_id}/semantic-matches",
    tags=["editorial-projects"],
)


@router.post("")
def propose(
    channel_profile_id: uuid.UUID,
    project_id: uuid.UUID,
    body: SemanticMatchRequest,
    request: Request,
):
    _authorize(request, channel_profile_id, write=True)
    try:
        return match_semantics(channel_profile_id, project_id, body, actor=control_actor(request))
    except ValueError as exc:
        raise _error(exc) from exc
    except EditorialBlocked as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/{run_id}")
def read(channel_profile_id: uuid.UUID, project_id: uuid.UUID, run_id: uuid.UUID, request: Request):
    _authorize(request, channel_profile_id)
    try:
        return get_semantic_match(channel_profile_id, project_id, run_id)
    except ValueError as exc:
        raise _error(exc) from exc
