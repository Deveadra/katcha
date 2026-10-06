"""Frozen Command Center operations for an attached Editorial project."""

from __future__ import annotations

import re
import uuid

from katcha.command_center_models import CommandActionProposal
from katcha.editorial.run_schemas import StartEditorialRun
from katcha.orchestration.client import get_temporal_client
from katcha.orchestration.editorial_dispatch import dispatch_editorial_run
from katcha.services.command_actions import ActionProposalSpec
from katcha.services.editorial_projects import get_project
from katcha.services.editorial_runs import control_run, get_run, start_run, workflow_id

_ACTIVE = {"queued", "running"}
_RECOVERABLE = {"blocked", "failed"}


def _project_evidence(evidence: list[dict[str, object]]) -> dict[str, object] | None:
    return next((item for item in evidence if item.get("kind") == "editorial_project"), None)


def _contains(prompt: str, pattern: str) -> bool:
    return bool(re.search(pattern, prompt, re.I))


def editorial_action_specs(
    prompt: str,
    evidence: list[dict[str, object]],
) -> list[ActionProposalSpec]:
    project = _project_evidence(evidence)
    if project is None:
        return []

    project_id = str(project["id"])
    revision = int(project.get("revision") or 0)
    latest = project.get("latest_run")
    latest_run = latest if isinstance(latest, dict) else {}
    status = str(latest_run.get("status") or "")
    run_id = latest_run.get("id")
    attempt = int(latest_run.get("attempt") or 0)

    if (
        run_id
        and attempt > 0
        and status in _RECOVERABLE
        and _contains(prompt, r"\b(resume|retry|continue|recover)\b")
    ):
        return [
            ActionProposalSpec(
                action_type="editorial_operation",
                label="Resume saved Editorial work",
                description=(
                    "Resume the exact failed/blocked Editorial attempt from its saved "
                    "checkpoints. The current script revision is revalidated first."
                ),
                payload={
                    "operation": "resume_run",
                    "project_id": project_id,
                    "editorial_run_id": str(run_id),
                    "expected_attempt": attempt,
                },
            )
        ]

    if (
        run_id
        and attempt > 0
        and status in _ACTIVE
        and _contains(prompt, r"\b(cancel|stop)\b")
    ):
        return [
            ActionProposalSpec(
                action_type="editorial_operation",
                label="Stop Editorial work",
                description=(
                    "Cancel further stages for the exact active Editorial attempt. "
                    "Saved checkpoints are retained."
                ),
                payload={
                    "operation": "cancel_run",
                    "project_id": project_id,
                    "editorial_run_id": str(run_id),
                    "expected_attempt": attempt,
                },
            )
        ]

    if status in _ACTIVE:
        return []

    direction = project.get("latest_direction")
    direction_run = direction if isinstance(direction, dict) else {}
    if (
        direction_run.get("id")
        and _contains(prompt, r"\b(render|create|make)\b")
        and _contains(prompt, r"\b(preview|render|visual plan|storyboard)\b")
    ):
        return [
            ActionProposalSpec(
                action_type="editorial_operation",
                label="Render saved visual plan",
                description=(
                    "Render the latest completed visual-direction receipt for this exact "
                    "script revision. The saved storyboard, assets, citations and audio "
                    "selection are revalidated before rendering."
                ),
                payload={
                    "operation": "render_saved_direction",
                    "project_id": project_id,
                    "direction_run_id": str(direction_run["id"]),
                    "expected_revision": revision,
                },
            )
        ]

    if _contains(prompt, r"\b(find|scout|source|start|run)\b") and _contains(
        prompt, r"\b(assets?|supporting media|supporting footage|b[- ]?roll)\b"
    ):
        return [
            ActionProposalSpec(
                action_type="editorial_operation",
                label="Scout supporting assets",
                description=(
                    "Start claim-directed supporting-media discovery for the current saved "
                    "script. Nothing is downloaded or approved for rendering automatically."
                ),
                payload={
                    "operation": "start_assets",
                    "project_id": project_id,
                    "expected_revision": revision,
                },
            )
        ]

    if _contains(prompt, r"\b(research|draft|write|start|run|generate)\b") and _contains(
        prompt, r"\b(script|research|evidence|claims?)\b"
    ):
        return [
            ActionProposalSpec(
                action_type="editorial_operation",
                label="Research and draft script",
                description=(
                    "Start source interpretation, bounded research, claim verification and "
                    "a cited script for the current project revision."
                ),
                payload={
                    "operation": "start_script",
                    "project_id": project_id,
                    "expected_revision": revision,
                },
            )
        ]

    if _contains(prompt, r"\b(analy[sz]e|analysis|inspect|prepare)\b") and _contains(
        prompt, r"\b(source|video|footage|frames?|transcript)\b"
    ):
        return [
            ActionProposalSpec(
                action_type="editorial_operation",
                label="Analyze project sources",
                description=(
                    "Prepare measured source analysis for this project. Katcha records "
                    "actual coverage rather than claiming unobserved frames."
                ),
                payload={
                    "operation": "start_analysis",
                    "project_id": project_id,
                    "expected_revision": revision,
                },
            )
        ]

    return []


async def _dispatch(row) -> dict[str, object]:
    result = {
        "editorial_run_id": str(row.id),
        "project_id": str(row.project_id),
        "workflow_id": workflow_id(row),
        "status": row.status,
        "stage": row.stage,
        "attempt": row.attempt,
        "target": row.options["target"],
    }
    try:
        await dispatch_editorial_run(await get_temporal_client(), row)
        result["dispatch"] = "confirmed"
    except Exception:
        result["dispatch"] = "pending"
        result["message"] = "Work is saved. The worker will retry dispatch automatically."
    return result


async def execute_editorial_operation(
    proposal: CommandActionProposal,
    *,
    actor: str,
) -> dict[str, object]:
    payload = dict(proposal.payload or {})
    operation = str(payload.get("operation") or "")
    project_id = uuid.UUID(str(payload["project_id"]))
    project = get_project(proposal.channel_profile_id, project_id)

    if operation in {"start_analysis", "start_script", "start_assets"}:
        target = {
            "start_analysis": "analysis",
            "start_script": "script",
            "start_assets": "assets",
        }[operation]
        expected_revision = int(payload["expected_revision"])
        request = StartEditorialRun(
            idempotency_key=proposal.idempotency_key,
            expected_revision=expected_revision,
            target=target,
        )
        row = start_run(
            proposal.channel_profile_id,
            project_id,
            request,
            actor=actor,
        )
        return await _dispatch(row)

    if operation in {"resume_run", "cancel_run"}:
        run_id = uuid.UUID(str(payload["editorial_run_id"]))
        expected_attempt = int(payload["expected_attempt"])
        # get_run proves project/channel ownership before the mutation is attempted.
        get_run(proposal.channel_profile_id, project_id, run_id)
        row = control_run(
            proposal.channel_profile_id,
            project_id,
            run_id,
            expected_attempt=expected_attempt,
            cancel=operation == "cancel_run",
        )
        if operation == "cancel_run":
            return {
                "editorial_run_id": str(row.id),
                "project_id": str(row.project_id),
                "workflow_id": workflow_id(row),
                "status": row.status,
                "stage": row.stage,
                "attempt": row.attempt,
                "cancelled": True,
            }
        return await _dispatch(row)

    if operation == "render_saved_direction":
        expected_revision = int(payload["expected_revision"])
        if project.revision != expected_revision:
            raise ValueError("Editorial project changed; reopen it before rendering")
        direction_id = uuid.UUID(str(payload["direction_run_id"]))
        direction = get_run(
            proposal.channel_profile_id,
            project_id,
            direction_id,
        )
        if (
            direction.status != "completed"
            or direction.options.get("target") != "direction"
            or direction.input_revision != expected_revision
            or not direction.artifacts.get("storyboard")
            or not direction.artifacts.get("direction_asset_run_id")
        ):
            raise ValueError("The selected visual direction is not render-ready")
        request = StartEditorialRun(
            idempotency_key=proposal.idempotency_key,
            expected_revision=expected_revision,
            target="render",
            direction_run_id=direction.id,
            asset_run_id=uuid.UUID(str(direction.artifacts["direction_asset_run_id"])),
            storyboard=direction.artifacts["storyboard"],
        )
        row = start_run(
            proposal.channel_profile_id,
            project_id,
            request,
            actor=actor,
        )
        return await _dispatch(row)

    raise ValueError(f"unsupported Editorial operation: {operation}")
