"""Read-only access to an exact cited frame under current project clearance."""

from __future__ import annotations

import uuid

from katcha.editorial.direction import resolve_shot_evidence
from katcha.editorial.provider import EditorialBlocked
from katcha.editorial.regions import frame_input
from katcha.editorial.render import current_manifest
from katcha.editorial.visual_compiler import compile_project_visuals
from katcha.editorial.visual_schemas import GroundedDirectionResult, StoryboardPlan
from katcha.services.editorial_projects import EditorialConflict, _digest
from katcha.services.editorial_runs import get_run


def inspect_frame(channel_id, project_id, run_id, shot_index):
    row = get_run(channel_id, project_id, run_id)
    if row.status != "completed":
        raise EditorialConflict("Complete this visual plan before inspecting its cited frames")
    if row.options.get("target") == "render" and row.options.get("direction_run_id"):
        manifest = current_manifest(row)
        direction = get_run(channel_id, project_id, uuid.UUID(row.options["direction_run_id"]))
    elif row.options.get("target") == "direction":
        direction = row
        manifest = compile_project_visuals(
            channel_id,
            project_id,
            row.input_revision,
            uuid.UUID(row.options["asset_run_id"]),
            StoryboardPlan.model_validate(row.artifacts.get("storyboard")),
        )
    else:
        raise EditorialConflict("This work has no saved directed-frame evidence")
    proof = row.artifacts.get("direction_shot_evidence") or {}
    if proof.get("storyboard_digest") != _digest(direction.artifacts.get("storyboard")):
        raise EditorialConflict("Saved frame citations no longer match the visual plan")
    shots = proof.get("shots", [])
    if not 0 <= shot_index < len(shots):
        raise EditorialConflict("This frame citation is unavailable; reload the visual plan")
    shot = shots[shot_index]
    try:
        resolved = resolve_shot_evidence(
            GroundedDirectionResult.model_validate(direction.artifacts["direction_proposal"]),
            direction.artifacts.get("direction_evidence", {}),
            manifest,
        )
        matches = [
            item
            for item in resolved
            if (item["beat_id"], item["candidate_id"]) == (shot["beat_id"], shot["candidate_id"])
        ]
        if len(matches) != 1 or any(
            shot[key] != matches[0][key] for key in ("observation", "sha256", "limitations")
        ):
            raise EditorialConflict("Saved frame evidence changed; plan visuals again")
        source = frame_input(direction, shot["candidate_id"], shot["observation"]["start_seconds"])
    except EditorialBlocked as exc:
        raise EditorialConflict(str(exc)) from exc
    scene = next(scene for scene in manifest.timeline if scene.beat_id == shot["beat_id"])
    index = next(i for i, use in enumerate(scene.media) if use.candidate_id == shot["candidate_id"])
    value = {
        "beat_id": shot["beat_id"],
        "candidate_id": shot["candidate_id"],
        "sample_seconds": shot["observation"]["start_seconds"],
        "observation": shot["observation"]["observation"],
        "coverage": "sampled_frames",
        "limitations": shot.get("limitations", []),
        "frozen": scene.media[index].freeze,
        # Overlays on a moving shot cannot be presented as tracked evidence.
        "overlays": [
            overlay.model_dump(mode="json")
            for overlay in scene.overlays
            if overlay.media_index == index
        ]
        if scene.media[index].freeze
        else [],
        "regions": shot.get("regions"),
        "image_key": source["image_key"],
    }
    return {**value, "evidence_digest": _digest({"view": value, "source": source})}
