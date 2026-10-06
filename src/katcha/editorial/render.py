"""Attempt-fenced local rendering with durable dispatch and reconciliation receipts."""

from __future__ import annotations

import uuid
from dataclasses import asdict

from sqlalchemy import update
from sqlalchemy.orm import Session

from katcha.db import session_scope
from katcha.editorial.direction_receipts import resolve_direction_receipt
from katcha.editorial.run_schemas import StartEditorialRun
from katcha.editorial.visual_compiler import compile_project_visuals
from katcha.editorial.visual_schemas import EditorialRenderManifest, StoryboardPlan
from katcha.editorial_models import EditorialRun
from katcha.rendering.client import (
    EditorialRenderRejected,
    recover_editorial_output,
    render_editorial,
)
from katcha.services.editorial_projects import EditorialConflict
from katcha.services.editorial_runs import ACTIVE, EditorialStopped, checkpoint


def current_manifest(
    row: EditorialRun, *, session: Session | None = None
) -> EditorialRenderManifest:
    if row.options.get("direction_run_id"):
        if session is None:
            with session_scope() as active_session:
                return current_manifest(row, session=active_session)
        receipt = resolve_direction_receipt(
            session,
            row.channel_profile_id,
            row.project_id,
            row.input_revision,
            StartEditorialRun.model_validate(row.options),
        )
        if any(row.artifacts.get(key) != value for key, value in receipt.items()) or (
            row.artifacts.get("direction_shot_evidence") != receipt.get("direction_shot_evidence")
        ):
            raise EditorialConflict("Saved frame citations changed; create a new preview")
    return compile_project_visuals(
        row.channel_profile_id,
        row.project_id,
        row.input_revision,
        uuid.UUID(row.options["asset_run_id"]) if row.options.get("asset_run_id") else None,
        StoryboardPlan.model_validate(row.options["storyboard"]),
        session=session,
    )


def claim_dispatch(run_id: str, attempt: int, manifest: EditorialRenderManifest) -> bool:
    """Serialize receipt creation on SQLite and PostgreSQL; only its creator may dispatch."""
    with session_scope() as session:
        locked = session.execute(
            update(EditorialRun)
            .where(
                EditorialRun.id == uuid.UUID(run_id),
                EditorialRun.attempt == attempt,
                EditorialRun.status.in_(ACTIVE),
            )
            .values(updated_at=EditorialRun.updated_at)
        )
        if locked.rowcount != 1:
            raise EditorialStopped("Render attempt was superseded or stopped")
        row = session.get(EditorialRun, uuid.UUID(run_id))
        frozen = manifest.model_dump(mode="json")
        if row.artifacts.get("render_dispatch"):
            if row.artifacts.get("render_manifest") != frozen:
                raise EditorialConflict("Storyboard or clearance changed after render dispatch")
            if row.artifacts["render_dispatch"].get("state") != "rejected":
                return False
        row.artifacts = {
            **row.artifacts,
            "render_manifest": frozen,
            "render_dispatch": {"state": "started", "attempt": attempt},
            "render_dispatch_history": [
                *row.artifacts.get("render_dispatch_history", []),
                {"state": "started", "attempt": attempt},
            ],
        }
        row.stage = "rendering"
    return True


def render_project(run_id: str, attempt: int) -> dict:
    try:
        row = checkpoint(run_id, attempt, stage="validating_storyboard")
        manifest = current_manifest(row)
        dispatch = claim_dispatch(run_id, attempt, manifest)
        # Recheck cancellation immediately before the external side effect.
        checkpoint(run_id, attempt, stage="rendering" if dispatch else "reconciling_render")
        result = render_editorial(manifest) if dispatch else recover_editorial_output(manifest)
        if result is None:
            raise EditorialConflict(
                "A previous render has uncertain completion. Resume after the renderer finishes "
                "to recover its verified output; this run will not launch duplicate work."
            )
        row = checkpoint(run_id, attempt, artifacts={"render_result": asdict(result)})
        # Rendering can take minutes. Clearance and revision must still match at promotion.
        if current_manifest(row) != manifest:
            raise EditorialConflict("Script, media or clearance changed during rendering")
        checkpoint(
            run_id,
            attempt,
            stage="render_ready_for_review",
            status="completed",
            artifacts={"requires_editorial_review": True},
        )
        return {
            "editorial_run_id": run_id,
            "status": "completed",
            "stage": "render_ready_for_review",
        }
    except EditorialRenderRejected:
        try:
            checkpoint(
                run_id,
                attempt,
                status="blocked",
                stage="render_rejected",
                error="Renderer rejected the request before work started. Check that the local "
                "renderer is configured and updated, then resume.",
                artifacts={"render_dispatch": {"state": "rejected", "attempt": attempt}},
            )
        except EditorialStopped:
            return {"editorial_run_id": run_id, "status": "stopped"}
        return {"editorial_run_id": run_id, "status": "blocked"}
    except EditorialStopped:
        return {"editorial_run_id": run_id, "status": "stopped"}
    except Exception as exc:
        message = (
            str(exc)
            if isinstance(exc, EditorialConflict)
            else (
                "Editorial rendering could not be verified. Check renderer availability and local "
                "backend configuration, then resume to check for a completed output. "
                "An uncertain dispatch is never repeated automatically."
            )
        )
        try:
            checkpoint(run_id, attempt, status="blocked", stage="render_blocked", error=message)
        except EditorialStopped:
            return {"editorial_run_id": run_id, "status": "stopped"}
        return {"editorial_run_id": run_id, "status": "blocked"}
