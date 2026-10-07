from __future__ import annotations

import asyncio
import uuid

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import and_, or_, select

from katcha.api.control_auth import control_actor
from katcha.api.editorial_projects import _authorize, _error
from katcha.api.schemas import CreateEditorialPublicationRequest, PublicationResponse
from katcha.db import session_scope
from katcha.editorial.review_schemas import ReviewEditorialRender
from katcha.editorial.run_schemas import (
    ReconcileNarrationBilling,
    ResumeEditorialRun,
    StartEditorialRun,
)
from katcha.editorial_models import EditorialRun
from katcha.orchestration.client import get_temporal_client
from katcha.orchestration.editorial_dispatch import dispatch_editorial_run
from katcha.services.editorial_projects import get_project
from katcha.services.editorial_reviews import review_render, review_status
from katcha.services.editorial_runs import control_run, get_run, start_run, workflow_id
from katcha.services.publications import register_editorial_publication

router = APIRouter(
    prefix="/v1/channels/{channel_profile_id}/editorial-projects/{project_id}/runs",
    tags=["editorial-projects"],
)


@router.post(
    "/{editorial_run_id}/publication",
    response_model=PublicationResponse,
    status_code=201,
)
def create_editorial_publication(
    channel_profile_id: uuid.UUID,
    project_id: uuid.UUID,
    editorial_run_id: uuid.UUID,
    body: CreateEditorialPublicationRequest,
    request: Request,
):
    """Stage one exact approved Editorial render for packaging before YouTube upload."""
    _authorize(request, channel_profile_id, write=True)
    from katcha.api.main import _require_youtube_execution

    _require_youtube_execution()
    try:
        run = get_run(channel_profile_id, project_id, editorial_run_id)
        return register_editorial_publication(
            run.id,
            youtube_connection_id=body.youtube_connection_id,
            title=body.title,
            description=body.description,
            tags=body.tags,
            category_id=body.category_id,
            privacy_status=body.privacy_status,
            publish_at=body.publish_at,
            notify_subscribers=body.notify_subscribers,
            made_for_kids=body.made_for_kids,
            contains_synthetic_media=body.contains_synthetic_media,
        )
    except ValueError as exc:
        raise _error(exc) from exc


@router.post("/{editorial_run_id}/narration-billing", status_code=201)
def reconcile_narration_billing(
    channel_profile_id: uuid.UUID, project_id: uuid.UUID, editorial_run_id: uuid.UUID,
    body: ReconcileNarrationBilling, request: Request,
):
    from katcha.editorial.generated_narration import reconcile_billing

    _authorize(request, channel_profile_id, write=True)
    try:
        return reconcile_billing(channel_profile_id, project_id, editorial_run_id, body,
                                 actor=control_actor(request))
    except ValueError as exc:
        raise _error(exc) from exc


@router.get("/{editorial_run_id}/review")
def render_review_status(
    channel_profile_id: uuid.UUID, project_id: uuid.UUID,
    editorial_run_id: uuid.UUID, request: Request,
):
    _authorize(request, channel_profile_id)
    try:
        return review_status(channel_profile_id, project_id, editorial_run_id)
    except ValueError as exc:
        raise _error(exc) from exc


@router.post("/{editorial_run_id}/review", status_code=201)
def save_render_review(
    channel_profile_id: uuid.UUID, project_id: uuid.UUID,
    editorial_run_id: uuid.UUID, body: ReviewEditorialRender, request: Request,
):
    _authorize(request, channel_profile_id, write=True)
    try:
        return review_render(
            channel_profile_id, project_id, editorial_run_id, body, actor=control_actor(request)
        )
    except ValueError as exc:
        raise _error(exc) from exc


def run_response(row: EditorialRun, *, include_artifacts: bool = True) -> dict:
    result = {
        "editorial_run_id": str(row.id),
        "project_id": str(row.project_id),
        "channel_profile_id": str(row.channel_profile_id),
        "workflow_id": workflow_id(row),
        "attempt": row.attempt,
        "input_revision": row.input_revision,
        "target": row.options["target"],
        "status": row.status,
        "stage": row.stage,
        "error": row.error,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }
    if include_artifacts:
        result["artifacts"] = row.artifacts
        if row.options["target"] == "narration" and row.status in {
            "blocked", "failed", "cancelled"
        }:
            from katcha.editorial.generated_narration import billing_holds

            result["artifacts"] = {**row.artifacts, "narration_billing": billing_holds(row)}
    return result


async def _dispatch(row: EditorialRun) -> dict:
    result = run_response(row)
    try:

        async def dispatch():
            return await dispatch_editorial_run(await get_temporal_client(), row)

        await asyncio.wait_for(dispatch(), timeout=10)
        result["dispatch"] = "confirmed"
    except Exception:
        result["dispatch"] = "pending"
        result["message"] = "Work is saved. The worker will retry dispatch automatically."
    return result


@router.post("", status_code=202)
async def start(
    channel_profile_id: uuid.UUID,
    project_id: uuid.UUID,
    body: StartEditorialRun,
    request: Request,
):
    _authorize(request, channel_profile_id, write=True)
    try:
        row = start_run(channel_profile_id, project_id, body, actor=control_actor(request))
    except ValueError as exc:
        raise _error(exc) from exc
    return await _dispatch(row)


@router.get("")
def list_runs(
    channel_profile_id: uuid.UUID,
    project_id: uuid.UUID,
    request: Request,
    offset: int = Query(0, ge=0, le=10000),
    limit: int = Query(20, ge=1, le=100),
    before: uuid.UUID | None = None,
):
    _authorize(request, channel_profile_id)
    try:
        get_project(channel_profile_id, project_id)
    except ValueError as exc:
        raise _error(exc) from exc
    with session_scope() as session:
        query = select(EditorialRun).where(
            EditorialRun.project_id == project_id,
            EditorialRun.channel_profile_id == channel_profile_id,
        )
        if before is not None:
            if offset:
                raise HTTPException(422, "Use either a history cursor or an offset")
            anchor = session.get(EditorialRun, before)
            if anchor is None or (anchor.project_id, anchor.channel_profile_id) != (
                project_id, channel_profile_id
            ):
                raise HTTPException(404, "History cursor not found in this project")
            query = query.where(or_(
                EditorialRun.created_at < anchor.created_at,
                and_(EditorialRun.created_at == anchor.created_at, EditorialRun.id > anchor.id),
            ))
        rows = session.scalars(
            query.order_by(EditorialRun.created_at.desc(), EditorialRun.id)
            .offset(offset).limit(limit)
        )
        return [run_response(row, include_artifacts=False) for row in rows]


@router.get("/{editorial_run_id}")
def detail(
    channel_profile_id: uuid.UUID,
    project_id: uuid.UUID,
    editorial_run_id: uuid.UUID,
    request: Request,
):
    _authorize(request, channel_profile_id)
    try:
        return run_response(get_run(channel_profile_id, project_id, editorial_run_id))
    except ValueError as exc:
        raise _error(exc) from exc


@router.post("/{editorial_run_id}/resume", status_code=202)
async def resume(
    channel_profile_id: uuid.UUID,
    project_id: uuid.UUID,
    editorial_run_id: uuid.UUID,
    body: ResumeEditorialRun,
    request: Request,
):
    _authorize(request, channel_profile_id, write=True)
    try:
        row = control_run(
            channel_profile_id,
            project_id,
            editorial_run_id,
            expected_attempt=body.expected_attempt,
            cancel=False,
        )
    except ValueError as exc:
        raise _error(exc) from exc
    return await _dispatch(row)


@router.post("/{editorial_run_id}/cancel")
def cancel(
    channel_profile_id: uuid.UUID,
    project_id: uuid.UUID,
    editorial_run_id: uuid.UUID,
    body: ResumeEditorialRun,
    request: Request,
):
    _authorize(request, channel_profile_id, write=True)
    try:
        row = control_run(
            channel_profile_id,
            project_id,
            editorial_run_id,
            expected_attempt=body.expected_attempt,
            cancel=True,
        )
    except ValueError as exc:
        raise _error(exc) from exc
    return {
        **run_response(row),
        "message": (
            "No further project stages will start. An in-flight shared media operation may finish."
        ),
    }
