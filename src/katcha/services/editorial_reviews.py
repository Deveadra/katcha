"""Serialized, replay-safe render review with current-clearance validation."""

from __future__ import annotations

import math
import uuid

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from katcha.db import session_scope
from katcha.editorial.render import current_manifest
from katcha.editorial.review_schemas import ReviewEditorialRender
from katcha.editorial.visual_schemas import EditorialRenderManifest
from katcha.editorial_models import EditorialProject, EditorialRenderReview, EditorialRun
from katcha.models import DomainEvent
from katcha.services.channel_profiles import ensure_active_profile
from katcha.services.editorial_projects import EditorialConflict, EditorialNotFound, _digest
from katcha.services.editorial_runs import get_run


def verified_manifest(
    row: EditorialRun, *, session: Session | None = None
) -> EditorialRenderManifest:
    """Shared preview/review/publication precondition; never trust a stored approval alone."""
    if (
        row.options.get("target") != "render"
        or row.status != "completed"
        or row.stage != "render_ready_for_review"
    ):
        raise EditorialConflict("This run has no completed editorial preview")
    manifest = current_manifest(row, session=session)
    if manifest.model_dump(mode="json") != row.artifacts.get("render_manifest"):
        raise EditorialConflict("Preview clearance or script revision changed; rebuild the render")
    result = row.artifacts.get("render_result") or {}
    duration = result.get("duration_seconds")
    metadata = result.get("metadata") or {}
    if (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(duration)
        or abs(duration - manifest.output_duration_seconds) > 1 / manifest.fps
        or result.get("output_key") != manifest.output_key
        or metadata.get("verified") is not True
        or metadata.get("composition") != "Editorial"
    ):
        raise EditorialConflict("This preview has no matching verified render receipt")
    return manifest


def approved_manifest(
    row: EditorialRun, *, session: Session
) -> tuple[EditorialRenderManifest, EditorialRenderReview]:
    """Return the exact currently cleared render and its latest approval receipt."""
    latest = session.scalar(
        select(EditorialRenderReview)
        .where(EditorialRenderReview.run_id == row.id)
        .order_by(EditorialRenderReview.sequence.desc())
        .limit(1)
    )
    if latest is None or latest.decision != "approve":
        raise EditorialConflict(
            "Editorial render needs a current operator approval before publication"
        )
    manifest = verified_manifest(row, session=session)
    if (
        latest.manifest_digest != _digest(row.artifacts["render_manifest"])
        or latest.result_digest != _digest(row.artifacts["render_result"])
    ):
        raise EditorialConflict("Editorial approval no longer matches the current render receipt")
    return manifest, latest


def _response(review: EditorialRenderReview) -> dict:
    return {
        "id": str(review.id),
        "sequence": review.sequence,
        "decision": review.decision,
        "note": review.note,
        "actor": review.actor,
        "created_at": review.created_at,
        "manifest_digest": review.manifest_digest,
    }


def review_render(
    channel_id: uuid.UUID,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    request: ReviewEditorialRender,
    *,
    actor: str,
) -> dict:
    review_id = uuid.uuid5(run_id, f"editorial-review:{request.idempotency_key}")
    digest = _digest(request.model_dump(mode="json"))
    with session_scope() as session:
        ensure_active_profile(session, channel_id)
        # Use the same project lock as draft saving. SQLite and PostgreSQL both
        # serialize decisions; a concurrent reviewer cannot overwrite a decision.
        locked = session.execute(
            update(EditorialProject)
            .where(
                EditorialProject.id == project_id,
                EditorialProject.channel_profile_id == channel_id,
            )
            .values(updated_at=EditorialProject.updated_at)
        )
        if locked.rowcount != 1:
            raise EditorialNotFound("Editorial project not found in this channel")
        row = session.get(EditorialRun, run_id)
        if row is None or row.project_id != project_id or row.channel_profile_id != channel_id:
            raise EditorialNotFound("Editorial run not found in this channel")
        previous = session.get(EditorialRenderReview, review_id)
        if previous is not None:
            if previous.request_digest != digest or previous.actor != actor:
                raise EditorialConflict("Review identity was already used for a different decision")
            # Replay returns the original receipt, not a claim of current approval.
            return _response(previous)
        latest = session.scalar(
            select(EditorialRenderReview)
            .where(EditorialRenderReview.run_id == run_id)
            .order_by(EditorialRenderReview.sequence.desc())
            .limit(1)
        )
        sequence = latest.sequence if latest else 0
        if sequence != request.expected_review_sequence:
            raise EditorialConflict(
                "Another review was saved. Refresh before changing the decision"
            )
        if row.input_revision != request.expected_revision:
            raise EditorialConflict("Review does not match the rendered script revision")
        if row.options.get("target") != "render" or row.status != "completed":
            raise EditorialConflict("Only a completed render can be reviewed")
        if request.decision == "approve":
            verified_manifest(row, session=session)
        elif not row.artifacts.get("render_manifest") or not row.artifacts.get("render_result"):
            raise EditorialConflict("This run has no render receipt to review")
        review = EditorialRenderReview(
            id=review_id,
            run_id=run_id,
            sequence=sequence + 1,
            request_digest=digest,
            manifest_digest=_digest(row.artifacts["render_manifest"]),
            result_digest=_digest(row.artifacts["render_result"]),
            decision=request.decision,
            note=request.note,
            actor=actor,
        )
        session.add(review)
        session.add(DomainEvent(
            aggregate_type="editorial_project", aggregate_id=str(project_id),
            event_type="editorial.render_reviewed",
            payload={
                "channel_profile_id": str(channel_id), "run_id": str(run_id),
                "review_id": str(review_id), "decision": request.decision,
                "actor": actor, "revision": request.expected_revision,
            },
        ))
        session.flush()
        session.refresh(review)
        return _response(review)


def review_status(channel_id: uuid.UUID, project_id: uuid.UUID, run_id: uuid.UUID) -> dict:
    row = get_run(channel_id, project_id, run_id)
    with session_scope() as session:
        reviews = list(session.scalars(
            select(EditorialRenderReview)
            .where(EditorialRenderReview.run_id == run_id)
            .order_by(EditorialRenderReview.sequence.desc()).limit(20)
        ))
        latest = reviews[0] if reviews else None
        status = latest.decision if latest else "unreviewed"
        blocker = None
        can_approve = False
        try:
            verified_manifest(row, session=session)
            can_approve = True
            if latest and (
                latest.manifest_digest != _digest(row.artifacts["render_manifest"])
                or latest.result_digest != _digest(row.artifacts["render_result"])
            ):
                raise EditorialConflict("Render receipt changed after review; review it again")
        except ValueError as exc:
            blocker = str(exc)
            if status == "approve":
                status = "invalidated"
        publication_available = False
        if blocker is None and latest is not None and latest.decision == "approve":
            try:
                approved_manifest(row, session=session)
                publication_available = True
            except ValueError as exc:
                blocker = str(exc)
                status = "invalidated"
        return {
            "status": status, "sequence": latest.sequence if latest else 0,
            "can_approve": can_approve, "blocker": blocker,
            "reviews": [_response(review) for review in reviews],
            "publication_available": publication_available,
        }
