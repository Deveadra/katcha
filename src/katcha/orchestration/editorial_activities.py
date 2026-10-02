from __future__ import annotations

import contextlib
import uuid

from sqlalchemy import select
from temporalio import activity

from katcha.config import get_settings
from katcha.db import session_scope
from katcha.models import Clip, ClipAnalysisRun, ClipFeature
from katcha.services.editorial_runs import EditorialStopped, checkpoint
from katcha.services.sources import register_source


@activity.defn
def editorial_begin(run_id: str, attempt: int) -> dict:
    row = checkpoint(run_id, attempt)
    settings = get_settings()
    return {
        "target": row.options.get("target", "analysis"),
        "source_count": len(row.artifacts["brief"]["source_urls"]),
        "ingest_queue": settings.temporal_task_queue,
        "analysis_queue": settings.temporal_analysis_task_queue,
    }


@activity.defn
def editorial_prepare_source(run_id: str, attempt: int, position: int) -> dict:
    row = checkpoint(run_id, attempt, stage="acquiring")
    url = row.artifacts["brief"]["source_urls"][position]
    sources = dict(row.artifacts.get("sources") or {})
    previous = sources.get(str(position))
    if previous:
        return previous
    binding = row.options.get("clip_bindings", {}).get(url)
    if binding:
        result = {"source_url": url, "clip_id": binding, "source_id": None}
    else:
        source = register_source(url)
        result = {
            "source_url": url,
            "source_id": str(source.id),
            "clip_id": str(source.clip_id) if source.clip_id else None,
        }
    sources[str(position)] = result
    checkpoint(run_id, attempt, artifacts={"sources": sources})
    return result


@activity.defn
def editorial_prepare_analysis(run_id: str, attempt: int, position: int, clip_id: str) -> dict:
    row = checkpoint(run_id, attempt, stage="analyzing")
    snapshots = dict(row.artifacts.get("source_snapshots") or {})
    if str(position) in snapshots:
        return {"complete": True}
    clip_uuid = uuid.UUID(clip_id)
    analysis_id = uuid.uuid5(uuid.UUID(run_id), f"local-analysis:{clip_id}")
    with session_scope() as session:
        clip = session.get(Clip, clip_uuid)
        if clip is None or not clip.duration_seconds or clip.duration_seconds <= 0:
            raise ValueError("Source media has no measured duration")
        features = session.get(ClipFeature, clip_uuid)
        reusable = bool(features and features.contact_sheet_key and features.local_features)
        if reusable:
            analysis = session.scalar(
                select(ClipAnalysisRun)
                .where(
                    ClipAnalysisRun.clip_id == clip_uuid,
                    ClipAnalysisRun.status == "completed",
                )
                .order_by(ClipAnalysisRun.completed_at.desc())
                .limit(1)
            )
            reusable = analysis is not None
            if analysis is not None:
                analysis_id = analysis.id
        if not reusable and session.get(ClipAnalysisRun, analysis_id) is None:
            session.add(
                ClipAnalysisRun(
                    id=analysis_id,
                    clip_id=clip_uuid,
                    workflow_id=f"editorial-local-analysis-{analysis_id}",
                    status="queued",
                    stage="queued",
                )
            )
    sources = dict(row.artifacts.get("sources") or {})
    sources[str(position)] = {
        **sources[str(position)],
        "clip_id": clip_id,
        "analysis_run_id": str(analysis_id),
    }
    checkpoint(run_id, attempt, artifacts={"sources": sources})
    return {"analysis_run_id": str(analysis_id), "reusable": reusable, "complete": False}


@activity.defn
def editorial_capture_source(run_id: str, attempt: int, position: int) -> dict:
    row = checkpoint(run_id, attempt, stage="analyzing")
    sources = row.artifacts["sources"]
    source = sources[str(position)]
    snapshots = dict(row.artifacts.get("source_snapshots") or {})
    if str(position) in snapshots:
        return {"saved": True, "reused": True}
    with session_scope() as session:
        clip = session.get(Clip, uuid.UUID(source["clip_id"]))
        analysis = session.get(ClipAnalysisRun, uuid.UUID(source["analysis_run_id"]))
        features = session.get(ClipFeature, clip.id) if clip else None
        if not clip or not analysis or analysis.status != "completed" or not features:
            raise ValueError("Source analysis has not completed; no evidence snapshot saved")
        snapshots[str(position)] = {
            **source,
            "sha256": clip.sha256,
            "storage_key": clip.storage_key,
            "duration_seconds": float(clip.duration_seconds),
            "width": clip.width,
            "height": clip.height,
            "contact_sheet_key": features.contact_sheet_key,
            "keyframe_keys": features.keyframe_keys,
            "transcript": features.transcript,
            "coverage": "sampled_frames",
            "local_features": features.local_features,
            # Never silently treat preexisting generic/fixture AI output as editorial evidence.
            "visual_observations": [],
            "native_video_analyzed": False,
        }
    checkpoint(run_id, attempt, artifacts={"source_snapshots": snapshots})
    return {"saved": True, "reused": False}


@activity.defn
def editorial_finish_intake(run_id: str, attempt: int) -> dict:
    row = checkpoint(run_id, attempt)
    if len(row.artifacts.get("source_snapshots", {})) != len(row.artifacts["brief"]["source_urls"]):
        raise ValueError("Source analysis is incomplete")
    status = "completed" if row.options.get("target", "analysis") == "analysis" else "running"
    checkpoint(run_id, attempt, stage="analysis_ready", status=status)
    return {"editorial_run_id": run_id, "status": status, "stage": "analysis_ready"}


@activity.defn
def editorial_fail(run_id: str, attempt: int, reason: str) -> None:
    # Persist bounded actionable state, not provider response bodies/secrets.
    with contextlib.suppress(EditorialStopped):
        checkpoint(run_id, attempt, status="failed", error=reason[:2000])


@activity.defn
def editorial_research_script(run_id: str, attempt: int) -> dict:
    from katcha.editorial.provider import EditorialBlocked
    from katcha.editorial.research import investigate_and_write

    try:
        return investigate_and_write(run_id, attempt)
    except EditorialBlocked as exc:
        # Only locally authored actionable messages; provider bodies never enter public errors.
        with contextlib.suppress(EditorialStopped):
            checkpoint(run_id, attempt, status="blocked", error=str(exc)[:2000])
        return {"editorial_run_id": run_id, "status": "blocked"}
    except EditorialStopped:
        return {"editorial_run_id": run_id, "status": "stopped"}
    except Exception:
        with contextlib.suppress(EditorialStopped):
            checkpoint(
                run_id,
                attempt,
                status="blocked",
                error=(
                    "Research could not validate a result. Saved evidence and provider receipts "
                    "are retained. Inspect incomplete calls before resuming; uncertain requests "
                    "will not be repeated automatically."
                ),
            )
        raise


EDITORIAL_ACTIVITIES = [
    editorial_begin,
    editorial_prepare_source,
    editorial_prepare_analysis,
    editorial_capture_source,
    editorial_finish_intake,
    editorial_research_script,
    editorial_fail,
]
