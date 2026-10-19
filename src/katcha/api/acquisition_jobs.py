"""Opt-in acquisition control plane; remote claims never promote media."""

from __future__ import annotations

import uuid
from collections.abc import Callable

from fastapi import APIRouter, HTTPException, Query, Request, Response

from katcha.acquisition.job_contracts import (
    ClaimJob,
    EnqueueJob,
    FailJob,
    HeartbeatJob,
    JobStatus,
    JobView,
    LeaseView,
    ReportJob,
)
from katcha.api.control_auth import (
    control_actor,
    control_allowed_channel_ids,
    control_principal_name,
    control_scopes,
    require_control_channel,
    require_control_scope,
    require_control_scope_any,
)
from katcha.config import get_settings
from katcha.services import acquisition_jobs as jobs

router = APIRouter(
    prefix="/v1/channels/{channel_profile_id}/acquisition-jobs", tags=["acquisition"]
)


def _call[T](fn: Callable[..., T], *args) -> T:
    try:
        return fn(*args)
    except jobs.JobNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except jobs.JobConflict as exc:
        raise HTTPException(409, str(exc)) from exc


def _enabled() -> None:
    if not get_settings().remote_acquisition_enabled:
        raise HTTPException(503, "Remote acquisition is not enabled")


def _worker(request: Request, channel_id: uuid.UUID) -> str:
    require_control_channel(request, channel_id)
    require_control_scope(request, "acquisition:work")
    name = control_principal_name(request)
    # Dedicated bearer principals only: no operator token, wildcard authority,
    # delegated AI identity, or interactive Cloudflare session on the host.
    if (
        not name
        or control_scopes(request) != {"acquisition:work"}
        or control_allowed_channel_ids(request) is None
        or getattr(request.state, "control_authentication_mode", None)
        in {"cloudflare_access", "goal_delegation"}
    ):
        raise HTTPException(
            403, "A dedicated channel-scoped acquisition worker principal is required"
        )
    return name


def _operator(request: Request, channel_id: uuid.UUID, scope: str) -> None:
    require_control_channel(request, channel_id)
    require_control_scope(request, scope)


def _reader(request: Request, channel_id: uuid.UUID) -> None:
    require_control_channel(request, channel_id)
    require_control_scope_any(request, "channels:read", "acquisition:work")
    if "acquisition:work" in control_scopes(request):
        _worker(request, channel_id)


@router.post("", response_model=JobView)
def enqueue(channel_profile_id: uuid.UUID, body: EnqueueJob, request: Request):
    _operator(request, channel_profile_id, "production:create")
    _enabled()
    return _call(jobs.enqueue_job, channel_profile_id, body, control_actor(request))


@router.get("", response_model=list[JobView])
def listing(
    channel_profile_id: uuid.UUID,
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    status: JobStatus | None = None,
):
    _reader(request, channel_profile_id)
    return jobs.list_jobs(channel_profile_id, limit=limit, status=status)


@router.get("/{job_id}", response_model=JobView)
def detail(channel_profile_id: uuid.UUID, job_id: uuid.UUID, request: Request):
    _reader(request, channel_profile_id)
    return _call(jobs.get_job, channel_profile_id, job_id)


@router.post("/{job_id}/claim", response_model=LeaseView)
def claim(
    channel_profile_id: uuid.UUID,
    job_id: uuid.UUID,
    body: ClaimJob,
    request: Request,
    response: Response,
):
    worker = _worker(request, channel_profile_id)
    _enabled()
    row, url, permission = _call(jobs.claim_job, channel_profile_id, job_id, worker, body)
    response.headers["Cache-Control"] = "no-store"
    return LeaseView(
        job=JobView.model_validate(row),
        claim_id=body.claim_id,
        fetch_url=url,
        staging_key=jobs.staging_key(row),
        permission=permission.model_dump(mode="json"),
    )


@router.post("/{job_id}/heartbeat", response_model=JobView)
def heartbeat(
    channel_profile_id: uuid.UUID,
    job_id: uuid.UUID,
    body: HeartbeatJob,
    request: Request,
):
    worker = _worker(request, channel_profile_id)
    _enabled()
    return _call(jobs.heartbeat_job, channel_profile_id, job_id, worker, body)


@router.post("/{job_id}/report", response_model=JobView)
def report(channel_profile_id: uuid.UUID, job_id: uuid.UUID, body: ReportJob, request: Request):
    worker = _worker(request, channel_profile_id)
    _enabled()
    return _call(jobs.report_job, channel_profile_id, job_id, worker, body)


@router.post("/{job_id}/fail", response_model=JobView)
def fail(channel_profile_id: uuid.UUID, job_id: uuid.UUID, body: FailJob, request: Request):
    # Fail/cancel remain available while disabled so work can drain safely.
    worker = _worker(request, channel_profile_id)
    return _call(jobs.fail_job, channel_profile_id, job_id, worker, body)


@router.post("/{job_id}/cancel", response_model=JobView)
def cancel(channel_profile_id: uuid.UUID, job_id: uuid.UUID, request: Request):
    _operator(request, channel_profile_id, "production:create")
    return _call(jobs.cancel_job, channel_profile_id, job_id, control_actor(request))


@router.post("/{job_id}/reconcile", response_model=JobView)
def reconcile(channel_profile_id: uuid.UUID, job_id: uuid.UUID, request: Request):
    _operator(request, channel_profile_id, "production:create")
    return _call(jobs.reconcile_expired_job, channel_profile_id, job_id, control_actor(request))
