"""Keep directed render requests bound to their reviewed planning receipts."""

from __future__ import annotations

from copy import deepcopy

from katcha.editorial.visual_schemas import StoryboardPlan
from katcha.editorial_models import EditorialRun
from katcha.services.editorial_projects import EditorialConflict, _digest


def resolve_direction_receipt(session, channel_id, project_id, revision, request):
    if request.direction_run_id is None:
        return {}
    row = session.get(EditorialRun, request.direction_run_id)
    if (
        row is None
        or row.channel_profile_id != channel_id
        or row.project_id != project_id
        or row.input_revision != revision
        or row.status != "completed"
        or row.options.get("target") != "direction"
    ):
        raise EditorialConflict("Choose a completed visual plan for this project and revision")
    artifacts = row.artifacts
    saved = artifacts.get("storyboard")
    if (
        saved is None
        or StoryboardPlan.model_validate(saved) != request.storyboard
        or artifacts.get("direction_asset_run_id") != str(request.asset_run_id)
    ):
        raise EditorialConflict("Visual plan changed; select the saved plan or render manual edits")
    evidence = artifacts.get("direction_shot_evidence")
    if evidence is not None and evidence.get("storyboard_digest") != _digest(saved):
        raise EditorialConflict("Frame citations no longer match the saved visual plan")
    return deepcopy(
        {
            "direction_run_id": str(row.id),
            "direction_storyboard_digest": _digest(saved),
            **({"direction_shot_evidence": evidence} if evidence is not None else {}),
        }
    )
