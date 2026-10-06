"""Authenticated Storyboard source-monitor evidence for acquired footage."""

from __future__ import annotations

import uuid

from katcha.db import session_scope
from katcha.editorial.assets import inspect_managed_candidate
from katcha.media.preprocess import sample_timestamps
from katcha.models import Clip, ClipFeature
from katcha.services.editorial_projects import EditorialConflict
from katcha.services.editorial_runs import get_run


def source_monitor(
    channel_id: uuid.UUID,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    candidate_id: str,
) -> dict[str, object]:
    row = get_run(channel_id, project_id, run_id)
    if row.options.get("target") != "acquire_assets":
        raise EditorialConflict("Source monitor requires an acquired-assets run")
    receipts = dict(row.artifacts.get("acquired_assets") or {})
    receipt = dict(receipts.get(candidate_id) or {})
    if not receipt:
        raise EditorialConflict("Selected footage is not part of this acquired-assets run")
    try:
        clip_id = uuid.UUID(str(receipt["clip_id"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise EditorialConflict("Selected footage has no valid managed clip receipt") from exc

    with session_scope() as session:
        current = inspect_managed_candidate(
            str(receipt.get("source_url") or ""),
            channel_id,
            session=session,
        )
        clip = session.get(Clip, clip_id)
        features = session.get(ClipFeature, clip_id)
        if (
            not current["production_eligible"]
            or current["clip_id"] != str(clip_id)
            or current["sha256"] != receipt.get("sha256")
            or clip is None
            or clip.storage_key != receipt.get("storage_key")
            or float(clip.duration_seconds or 0) != float(receipt.get("duration_seconds") or 0)
        ):
            raise EditorialConflict("Acquired footage changed or needs current clearance")
        if features is None or not features.contact_sheet_key or not features.keyframe_keys:
            raise EditorialConflict(
                "Selected footage has no sampled frames; analyze it before using the source monitor"
            )
        expected_prefix = f"analysis/{clip.sha256[:2]}/{clip.sha256}/"
        if (
            features.contact_sheet_key != f"{expected_prefix}contact-sheet.jpg"
            or any(not key.startswith(f"{expected_prefix}frames/") for key in features.keyframe_keys)
        ):
            raise EditorialConflict("Selected footage frame evidence has an invalid storage identity")
        frame_count = len(features.keyframe_keys)
        duration = float(clip.duration_seconds or 0)
        contact_sheet_key = features.contact_sheet_key

    selection = next(
        (
            dict(item)
            for item in row.artifacts.get("asset_selection") or []
            if item.get("id") == candidate_id
        ),
        {},
    )
    return {
        "candidate_id": candidate_id,
        "title": str(selection.get("title") or candidate_id),
        "source_url": str(receipt.get("source_url") or ""),
        "clip_id": str(clip_id),
        "sha256": str(receipt["sha256"]),
        "duration_seconds": duration,
        "frame_count": frame_count,
        "sample_times": sample_timestamps(duration, frame_count),
        "contact_sheet_key": contact_sheet_key,
        "coverage": "sampled_frames",
        "limitation": (
            "These frames are samples, not continuous playback. Verify exact motion and timing "
            "in the rendered preview before approval."
        ),
    }
