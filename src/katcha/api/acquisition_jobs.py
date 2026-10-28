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
    LeaseMessage,
    LeaseView,
    PartView,
    ReleaseSource,
    ReportJob,
    TransferView,
    UploadPart,
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
from katcha.services import acquisition_handoff as handoff
from katcha.services import acquisition_jobs as jobs
from katcha.services import acquisition_recovery as recovery

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
        staging_key=handoff.staging_key_for_job(row),
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


@router.post("/{job_id}/upload", response_model=TransferView)
def upload(
    channel_profile_id: uuid.UUID,
    job_id: uuid.UUID,
    body: ReportJob,
    request: Request,
    response: Response,
):
    worker = _worker(request, channel_profile_id)
    _enabled()
    response.headers["Cache-Control"] = "no-store"
    return _call(handoff.prepare_upload, channel_profile_id, job_id, worker, body)


@router.post("/{job_id}/upload/parts/{part_number}", response_model=PartView)
def upload_part(
    channel_profile_id: uuid.UUID,
    job_id: uuid.UUID,
    part_number: int,
    body: UploadPart,
    request: Request,
    response: Response,
):
    worker = _worker(request, channel_profile_id)
    _enabled()
    response.headers["Cache-Control"] = "no-store"
    return _call(
        handoff.authorize_part,
        channel_profile_id,
        job_id,
        worker,
        body,
        part_number,
        body.content_md5,
    )


@router.post("/{job_id}/upload/complete", response_model=JobView)
def upload_complete(
    channel_profile_id: uuid.UUID, job_id: uuid.UUID, body: LeaseMessage, request: Request
):
    worker = _worker(request, channel_profile_id)
    _enabled()
    return _call(handoff.complete_upload, channel_profile_id, job_id, worker, body)


@router.post("/{job_id}/upload/recover", response_model=LeaseView)
def recover_upload(
    channel_profile_id: uuid.UUID,
    job_id: uuid.UUID,
    body: ReportJob,
    request: Request,
    response: Response,
):
    worker = _worker(request, channel_profile_id)
    _enabled()
    row, url, permission = _call(handoff.recover_upload, channel_profile_id, job_id, worker, body)
    response.headers["Cache-Control"] = "no-store"
    return LeaseView(
        job=JobView.model_validate(row),
        claim_id=body.claim_id,
        fetch_url=url,
        staging_key=handoff.staging_key_for_job(row),
        permission=permission.model_dump(mode="json"),
    )


@router.post("/{job_id}/validate", response_model=JobView)
def validate_upload(channel_profile_id: uuid.UUID, job_id: uuid.UUID, request: Request):
    _operator(request, channel_profile_id, "production:create")
    # Validation makes no new provider request and is allowed while draining.
    return _call(handoff.finalize_job, channel_profile_id, job_id)


@router.post("/{job_id}/upload/cleanup", response_model=JobView)
def cleanup_upload(channel_profile_id: uuid.UUID, job_id: uuid.UUID, request: Request):
    _operator(request, channel_profile_id, "production:create")
    return _call(handoff.cleanup_transfer, channel_profile_id, job_id)


@router.post("/{job_id}/release-source", response_model=JobView)
def release_source(
    channel_profile_id: uuid.UUID, job_id: uuid.UUID, body: ReleaseSource, request: Request
):
    _operator(request, channel_profile_id, "production:create")
    return _call(handoff.release_source, channel_profile_id, job_id, control_actor(request))


# Human verification is deliberately separate from AI goal browser tools. A goal
# delegation may not read credentials/screenshots or attest an operator action.
def _human_operator(request: Request, channel_id: uuid.UUID):
    _operator(request, channel_id, "production:create")
    if getattr(request.state, "control_authentication_mode", None) == "goal_delegation":
        raise HTTPException(403, "Human verification requires an operator session")


def _human_enabled():
    from katcha.services.acquisition_recovery import configured

    _enabled()
    if not configured():
        raise HTTPException(503, "Human verification is not configured; contact the operator")


def _private(response: Response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"


@router.get("/{job_id}/human")
def human_detail(
    channel_profile_id: uuid.UUID, job_id: uuid.UUID, request: Request, response: Response
):
    _human_operator(request, channel_profile_id)
    _private(response)
    return _call(recovery.get_challenge, channel_profile_id, job_id)


@router.post("/{job_id}/human/commands")
def human_command(
    channel_profile_id: uuid.UUID,
    job_id: uuid.UUID,
    body: recovery.RecoveryAction,
    request: Request,
    response: Response,
):
    from pydantic import ValidationError

    _human_operator(request, channel_profile_id)
    _human_enabled()
    _private(response)
    try:
        return _call(
            recovery.enqueue_command, channel_profile_id, job_id, body, control_actor(request)
        )
    except ValidationError:
        # Pydantic errors can contain submitted passwords. Never echo input.
        raise HTTPException(422, "Invalid browser action; refresh the view and try again") from None


@router.get("/{job_id}/human/commands/{command_id}")
def human_command_result(
    channel_profile_id: uuid.UUID,
    job_id: uuid.UUID,
    command_id: uuid.UUID,
    request: Request,
    response: Response,
):
    _human_operator(request, channel_profile_id)
    _private(response)
    return _call(recovery.get_command, channel_profile_id, job_id, command_id)


@router.post("/{job_id}/human/retry")
def human_retry(
    channel_profile_id: uuid.UUID,
    job_id: uuid.UUID,
    body: recovery.RecoveryRetry,
    request: Request,
    response: Response,
):
    _human_operator(request, channel_profile_id)
    _human_enabled()
    _private(response)
    return _call(recovery.request_retry, channel_profile_id, job_id, body, control_actor(request))


@router.post("/{job_id}/human/poll")
def human_poll(
    channel_profile_id: uuid.UUID,
    job_id: uuid.UUID,
    body: LeaseMessage,
    request: Request,
    response: Response,
):
    worker = _worker(request, channel_profile_id)
    _human_enabled()
    _private(response)
    return _call(recovery.poll, channel_profile_id, job_id, worker, body)


@router.post("/{job_id}/human/result")
def human_report(
    channel_profile_id: uuid.UUID,
    job_id: uuid.UUID,
    body: recovery.RecoveryResult,
    request: Request,
    response: Response,
):
    worker = _worker(request, channel_profile_id)
    _human_enabled()
    _private(response)
    return _call(recovery.complete_command, channel_profile_id, job_id, worker, body)


@router.post("/{job_id}/human/unavailable")
def human_unavailable(
    channel_profile_id: uuid.UUID,
    job_id: uuid.UUID,
    body: recovery.RecoveryUnavailable,
    request: Request,
    response: Response,
):
    worker = _worker(request, channel_profile_id)
    _private(response)
    return _call(recovery.unavailable, channel_profile_id, job_id, worker, body)


@router.post("/{job_id}/human/resume", response_model=LeaseView)
def human_resume(
    channel_profile_id: uuid.UUID,
    job_id: uuid.UUID,
    body: LeaseMessage,
    request: Request,
    response: Response,
):
    worker = _worker(request, channel_profile_id)
    _human_enabled()
    _private(response)
    row, url, permission = _call(recovery.resume, channel_profile_id, job_id, worker, body)
    return LeaseView(
        job=JobView.model_validate(row),
        claim_id=body.claim_id,
        fetch_url=url,
        staging_key=handoff.staging_key_for_job(row),
        permission=permission.model_dump(mode="json"),
    )
