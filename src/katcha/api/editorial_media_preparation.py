"""Scoped preparation checkpoints in the existing Editorial project API."""

import logging
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from katcha.api.control_auth import control_actor
from katcha.api.editorial_projects import _authorize, _error
from katcha.editorial.media_preparation_schemas import (
    ReconcileMediaPreparation,
    StartMediaPreparation,
)
from katcha.services import editorial_media_preparation as preparation

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/v1/channels/{channel_profile_id}/editorial-projects/{project_id}/media-preparation",
    tags=["editorial-projects"],
)


@router.post("", status_code=202)
def start(
    channel_profile_id: uuid.UUID,
    project_id: uuid.UUID,
    body: StartMediaPreparation,
    request: Request,
) -> dict[str, Any]:
    _authorize(request, channel_profile_id, write=True)
    try:
        row = preparation.start_preparation(
            channel_profile_id,
            project_id,
            body,
            actor=control_actor(request),
        )
        return preparation.response(row)
    except ValueError as exc:
        raise _error(exc) from exc


@router.get("")
def latest(
    channel_profile_id: uuid.UUID, project_id: uuid.UUID, request: Request
) -> dict[str, Any] | None:
    _authorize(request, channel_profile_id)
    try:
        row = preparation.latest_preparation(channel_profile_id, project_id)
        return preparation.response(row) if row else None
    except ValueError as exc:
        raise _error(exc) from exc


@router.get("/{run_id}")
def detail(
    channel_profile_id: uuid.UUID, project_id: uuid.UUID, run_id: uuid.UUID, request: Request
) -> dict[str, Any]:
    _authorize(request, channel_profile_id)
    try:
        return preparation.response(
            preparation.get_preparation(
                channel_profile_id,
                project_id,
                run_id,
            )
        )
    except ValueError as exc:
        raise _error(exc) from exc


@router.post("/{run_id}/resume")
def resume(
    channel_profile_id: uuid.UUID, project_id: uuid.UUID, run_id: uuid.UUID, request: Request
) -> dict[str, Any]:
    _authorize(request, channel_profile_id, write=True)
    try:
        return preparation.response(
            preparation.resume_preparation(
                channel_profile_id,
                project_id,
                run_id,
            )
        )
    except ValueError as exc:
        raise _error(exc) from exc
    except Exception as exc:
        logger.exception("Media preparation checkpoint paused: %s", run_id)
        raise HTTPException(
            503, "Preparation paused. Continue saved work to recover safely."
        ) from exc


@router.post("/{run_id}/reconcile")
def reconcile(
    channel_profile_id: uuid.UUID,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    body: ReconcileMediaPreparation,
    request: Request,
) -> dict[str, Any]:
    _authorize(request, channel_profile_id, write=True)
    try:
        return preparation.response(
            preparation.reconcile_preparation(
                channel_profile_id,
                project_id,
                run_id,
                body,
            )
        )
    except ValueError as exc:
        raise _error(exc) from exc
