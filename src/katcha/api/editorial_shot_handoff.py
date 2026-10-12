"""Approved shot handoff using existing authenticated Editorial/Storyboard scope."""

import uuid

from fastapi import APIRouter, Request

from katcha.api.control_auth import control_actor
from katcha.api.editorial_projects import _authorize, _error, _storyboard
from katcha.editorial.shot_handoff_schemas import ApplyApprovedShot
from katcha.editorial.storyboard_schemas import StoryboardWorkspaceResponse
from katcha.services.editorial_shot_handoff import apply_approved_shot

router = APIRouter(
    prefix="/v1/channels/{channel_profile_id}/editorial-projects/{project_id}",
    tags=["editorial-projects"],
)


@router.post(
    "/storyboard/approved-shot", response_model=StoryboardWorkspaceResponse, status_code=201
)
def approved_shot(
    channel_profile_id: uuid.UUID, project_id: uuid.UUID, body: ApplyApprovedShot, request: Request
) -> StoryboardWorkspaceResponse:
    _authorize(request, channel_profile_id, write=True)
    try:
        return _storyboard(
            apply_approved_shot(channel_profile_id, project_id, body, actor=control_actor(request))
        )
    except ValueError as exc:
        raise _error(exc) from exc
