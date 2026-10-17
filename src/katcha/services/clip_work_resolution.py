"""Safely resolve clip deletion blockers against authoritative Temporal executions.

Never release a clip just because a DB status appears stale. A cancellation
request does not imply workers have stopped or that media is safe to delete.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from temporalio.client import RPCError, RPCStatusCode, WorkflowExecutionStatus

from katcha.db import session_scope
from katcha.longform_models import Compilation, CompilationSegment
from katcha.models import Clip, DomainEvent
from katcha.orchestration.client import get_temporal_client
from katcha.production_models import Production
from katcha.publishing_models import Publication
from katcha.render_models import RenderAttempt
from katcha.short_episode_models import ShortEpisode, ShortEpisodeItem
from katcha.services.clip_lifecycle import (
    _ACTIVE_COMPILATION_STATUSES,
    _ACTIVE_EPISODE_STATUSES,
    _ACTIVE_PRODUCTION_STATUSES,
)

WorkKind = Literal["production", "short_episode", "compilation"]
_MODELS = {
    "production": Production,
    "short_episode": ShortEpisode,
    "compilation": Compilation,
}
_ACTIVE = {
    "production": _ACTIVE_PRODUCTION_STATUSES,
    "short_episode": _ACTIVE_EPISODE_STATUSES,
    "compilation": _ACTIVE_COMPILATION_STATUSES,
}
_STOPPED = {
    WorkflowExecutionStatus.CANCELED,
    WorkflowExecutionStatus.TERMINATED,
    WorkflowExecutionStatus.FAILED,
    WorkflowExecutionStatus.TIMED_OUT,
}
_ALLOWED_RENDER_TERMINAL = {"completed", "succeeded", "failed", "dead_letter", "cancelled"}


class WorkStopConflict(ValueError):
    pass


@dataclass(frozen=True)
class WorkStopResult:
    kind: WorkKind
    id: uuid.UUID
    state: str
    status: str
    detail: str


def _load_work(session, clip_id: uuid.UUID, kind: WorkKind, work_id: uuid.UUID):
    if session.get(Clip, clip_id) is None:
        raise LookupError("clip not found")
    row = session.get(_MODELS[kind], work_id)
    if row is None:
        raise LookupError("production work not found")
    if kind == "production":
        attached = row.clip_id == clip_id
    elif kind == "short_episode":
        attached = session.scalar(
            select(ShortEpisodeItem.id).where(
                ShortEpisodeItem.short_episode_id == work_id,
                ShortEpisodeItem.clip_id == clip_id,
            )
        ) is not None
    else:
        attached = session.scalar(
            select(CompilationSegment.id).where(
                CompilationSegment.compilation_id == work_id,
                CompilationSegment.clip_id == clip_id,
            )
        ) is not None
    if not attached:
        raise LookupError("work does not reference this clip")
    return row


def _execution_ids(kind: WorkKind, workflow_id: str) -> list[str]:
    if not workflow_id:
        raise WorkStopConflict("workflow identity is missing; manual reconciliation required")
    if kind == "short_episode":
        # Ranked editorial starts separate executions per stage. Checking only
        # the planning workflow could release footage during a render.
        return [
            workflow_id,
            *(f"{workflow_id}-editorial-{stage}" for stage in ("script", "voice", "render")),
        ]
    return [workflow_id]


def _no_publication_consumers(session, kind: WorkKind, work_id: uuid.UUID) -> None:
    # Publication handoffs execute independently and must be resolved first.
    column = getattr(Publication, f"{kind}_id")
    publications = session.scalars(select(Publication).where(column == work_id))
    if any(row.status not in {"failed", "rejected", "cancelled"} for row in publications):
        raise WorkStopConflict("a publication still references this work; resolve it first")


def _no_media_consumers(session, kind: WorkKind, work_id: uuid.UUID) -> None:
    _no_publication_consumers(session, kind, work_id)
    # Rendering may still be active when an operator *requests* cancellation.
    # Block release, not the cancellation request itself, until it is terminal.
    if kind in {"production", "short_episode"}:
        render_column = getattr(RenderAttempt, f"{kind}_id")
        render_attempts = session.scalars(
            select(RenderAttempt).where(render_column == work_id)
        )
        if any(row.status not in _ALLOWED_RENDER_TERMINAL for row in render_attempts):
            raise WorkStopConflict(
                "render attempts are not terminal; media remains protected"
            )


async def stop_clip_work(
    clip_id: uuid.UUID,
    kind: WorkKind,
    work_id: uuid.UUID,
    *,
    expected_status: str,
    actor: str,
) -> WorkStopResult:
    """Request cancellation, or finalize only after observed terminal execution."""
    with session_scope() as session:
        row = _load_work(session, clip_id, kind, work_id)
        if row.status not in _ACTIVE[kind]:
            return WorkStopResult(kind, work_id, "already_resolved", row.status,
                                  "This work is no longer blocking the clip.")
        if row.status != expected_status:
            raise WorkStopConflict("work changed since inspection; refresh before stopping")
        workflow_ids = _execution_ids(kind, row.workflow_id)
        _no_publication_consumers(session, kind, work_id)

    client = await get_temporal_client()
    observed = []
    running = []
    for execution_id in workflow_ids:
        handle = client.get_workflow_handle(execution_id)
        try:
            description = await handle.describe()
        except RPCError as exc:
            if exc.status == RPCStatusCode.NOT_FOUND:
                continue
            raise
        observed.append(description.status)
        if description.status == WorkflowExecutionStatus.RUNNING:
            running.append(handle)

    if running:
        # Cancellation is cooperative. Never update DB or unlock deletion here.
        for handle in running:
            await handle.cancel()
        return WorkStopResult(
            kind, work_id, "cancellation_requested", expected_status,
            "Temporal cancellation requested; wait for confirmed shutdown, then retry.",
        )
    if not observed:
        raise WorkStopConflict(
            "No Temporal execution was found; manual reconciliation is required"
        )
    if any(state not in (_STOPPED | {WorkflowExecutionStatus.COMPLETED})
           for state in observed):
        raise WorkStopConflict("workflow shutdown could not be verified")
    if all(state == WorkflowExecutionStatus.COMPLETED for state in observed):
        if expected_status not in {"review", "render_review", "rendered"}:
            raise WorkStopConflict(
                "Temporal completed but the work record remains active; "
                "inspect the result before changing its state"
            )
        new_status = "rejected"
    else:
        new_status = "cancelled"

    # Lock and revalidate again after the remote API check. Explicitly do not
    # update if the work, render attempts or publication state changed.
    with session_scope() as session:
        model = _MODELS[kind]
        row = session.scalar(
            select(model).where(model.id == work_id).with_for_update()
        )
        if row is None:
            raise LookupError("production work not found")
        _load_work(session, clip_id, kind, work_id)
        if row.status != expected_status:
            raise WorkStopConflict("work status changed during shutdown confirmation")
        _no_media_consumers(session, kind, work_id)
        row.status = new_status
        row.stage = new_status
        session.add(DomainEvent(
            aggregate_type=kind,
            aggregate_id=str(work_id),
            event_type=f"{kind}.operator_{new_status}",
            payload={
                "clip_id": str(clip_id),
                "actor": actor,
                "previous_status": expected_status,
                "status": new_status,
                "verified_temporal_terminal": [state.name for state in observed],
            },
        ))
    return WorkStopResult(
        kind, work_id, "resolved", new_status,
        "The recorded workflow is terminal and no active consumers were found.",
    )
