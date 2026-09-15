from __future__ import annotations

import shutil
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from temporalio import activity

from katcha.ai.providers import analyze_contact_sheet, analyze_full_video
from katcha.config import get_settings
from katcha.db import session_scope
from katcha.domain import AnalysisStatus, ClipStatus, require_clip_transition
from katcha.integrations.storage import ObjectStore
from katcha.media.preprocess import preprocess_video
from katcha.media.transcription import TranscriptResult, get_transcriber
from katcha.models import Clip, ClipAnalysisRun, ClipFeature, DomainEvent
from katcha.services.scoring import score_candidate


def _has_audio(metadata: dict[str, Any]) -> bool:
    streams = metadata.get("streams")
    return isinstance(streams, list) and any(
        isinstance(stream, dict) and stream.get("codec_type") == "audio" for stream in streams
    )


def _merge_source_metrics(clip: Clip) -> dict[str, Any]:
    numeric_keys = {
        "view_count",
        "views",
        "play_count",
        "like_count",
        "likes",
        "comment_count",
        "comments",
    }
    merged: dict[str, Any] = {}
    for source in clip.sources:
        for key, value in (source.source_metadata or {}).items():
            if key not in numeric_keys:
                continue
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                continue
            existing = merged.get(key)
            if existing is None or numeric > float(existing):
                merged[key] = numeric
    return merged


def _advance_local_status(clip: Clip) -> None:
    current = ClipStatus(clip.status)
    if current == ClipStatus.INGESTED:
        require_clip_transition(current, ClipStatus.NORMALIZED)
        clip.status = ClipStatus.NORMALIZED.value
        current = ClipStatus.NORMALIZED
    if current == ClipStatus.NORMALIZED:
        require_clip_transition(current, ClipStatus.ANALYZED)
        clip.status = ClipStatus.ANALYZED.value


@activity.defn
def build_local_intelligence(run_id: str) -> dict[str, object]:
    run_uuid = uuid.UUID(run_id)
    settings = get_settings()
    with session_scope() as session:
        run = session.get(ClipAnalysisRun, run_uuid)
        if run is None:
            raise ValueError(f"analysis run not found: {run_id}")
        clip = session.get(Clip, run.clip_id)
        if clip is None:
            raise ValueError(f"clip not found: {run.clip_id}")
        run.status = AnalysisStatus.RUNNING.value
        run.stage = "local_intelligence"
        run.error = None
        if run.started_at is None:
            run.started_at = datetime.now(UTC)
        clip_id = clip.id
        clip_sha = clip.sha256
        storage_key = clip.storage_key
        extension = clip.extension or "mp4"
        duration = float(clip.duration_seconds or 0)
        media_metadata = dict(clip.media_metadata or {})

    work_dir = settings.work_dir / "analysis" / run_id
    shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    raw_path = work_dir / f"input.{extension}"
    derived_dir = work_dir / "derived"

    try:
        store = ObjectStore()
        store.ensure_bucket()
        store.download_file(storage_key, raw_path)
        result = preprocess_video(
            raw_path,
            derived_dir,
            duration_seconds=max(duration, 0.001),
            frame_count=settings.analysis_frame_count,
        )

        frame_keys: list[str] = []
        for index, frame_path in enumerate(result.frame_paths):
            key = store.analysis_key(clip_sha, f"frames/frame-{index:02d}.jpg")
            if not store.exists(key):
                store.put_file(frame_path, key, content_type="image/jpeg")
            frame_keys.append(key)

        contact_sheet_key = store.analysis_key(clip_sha, "contact-sheet.jpg")
        if not store.exists(contact_sheet_key):
            store.put_file(result.contact_sheet_path, contact_sheet_key, content_type="image/jpeg")

        transcript = TranscriptResult(text="", language=None, confidence=None, segment_count=0)
        has_audio = _has_audio(media_metadata)
        if has_audio:
            transcript = get_transcriber().transcribe(raw_path)

        local_features: dict[str, Any] = {
            "duration_seconds": duration,
            "frame_count": len(frame_keys),
            "mean_hash_distance": result.mean_hash_distance,
            "has_audio": has_audio,
            "transcript_word_count": len(transcript.text.split()),
            "transcript_segments": transcript.segment_count,
        }

        with session_scope() as session:
            run = session.get(ClipAnalysisRun, run_uuid)
            clip = session.get(Clip, clip_id)
            if run is None or clip is None:
                raise RuntimeError("analysis state disappeared during processing")
            features = session.get(ClipFeature, clip_id)
            if features is None:
                features = ClipFeature(clip_id=clip_id)
                session.add(features)
            features.contact_sheet_key = contact_sheet_key
            features.keyframe_keys = frame_keys
            features.perceptual_hashes = result.perceptual_hashes
            features.transcript = transcript.text or None
            features.transcript_language = transcript.language
            features.transcript_confidence = (
                Decimal(str(transcript.confidence)) if transcript.confidence is not None else None
            )
            features.local_features = local_features
            _advance_local_status(clip)
            session.add(
                DomainEvent(
                    aggregate_type="clip",
                    aggregate_id=str(clip.id),
                    event_type="clip.local_intelligence_ready",
                    payload={
                        "clip_id": str(clip.id),
                        "analysis_run_id": run_id,
                        "contact_sheet_key": contact_sheet_key,
                        "frame_count": len(frame_keys),
                    },
                )
            )

        return {
            "run_id": run_id,
            "clip_id": str(clip_id),
            "frame_count": len(frame_keys),
            "has_audio": has_audio,
        }
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


@activity.defn
def bulk_vision_analysis(run_id: str) -> dict[str, object]:
    run_uuid = uuid.UUID(run_id)
    settings = get_settings()
    with session_scope() as session:
        run = session.get(ClipAnalysisRun, run_uuid)
        if run is None:
            raise ValueError(f"analysis run not found: {run_id}")
        features = session.get(ClipFeature, run.clip_id)
        if features is None or not features.contact_sheet_key:
            raise RuntimeError("contact sheet must exist before bulk vision")
        run.stage = "bulk_vision"
        contact_sheet_key = features.contact_sheet_key
        transcript = features.transcript

    image_bytes = ObjectStore().get_bytes(contact_sheet_key)
    result = analyze_contact_sheet(
        image_bytes,
        transcript,
        reference_id=run_id,
        settings=settings,
    )
    payload = result.value.model_dump(mode="json")
    deep_available = bool(settings.gemini_api_key)

    with session_scope() as session:
        run = session.get(ClipAnalysisRun, run_uuid)
        features = session.get(ClipFeature, run.clip_id) if run is not None else None
        if run is None or features is None:
            raise RuntimeError("analysis state disappeared during bulk vision")
        ai_features = dict(features.ai_features or {})
        ai_features["bulk"] = payload
        ai_features["bulk_provider"] = result.target.provider
        ai_features["bulk_model"] = result.target.model
        features.ai_features = ai_features
        if result.value.requires_deep_video:
            run.escalation_reason = (
                result.value.deep_video_reason or "bulk vision requested escalation"
            )
            if not deep_available:
                run.escalation_reason += "; Gemini API key is unavailable, bulk result retained"
        else:
            run.escalation_reason = None

    return {
        "requires_deep_video": result.value.requires_deep_video,
        "deep_available": deep_available,
        "provider": result.target.provider,
        "model": result.target.model,
    }


@activity.defn
def deep_video_analysis(run_id: str) -> dict[str, object]:
    run_uuid = uuid.UUID(run_id)
    settings = get_settings()
    with session_scope() as session:
        run = session.get(ClipAnalysisRun, run_uuid)
        if run is None:
            raise ValueError(f"analysis run not found: {run_id}")
        clip = session.get(Clip, run.clip_id)
        features = session.get(ClipFeature, run.clip_id)
        if clip is None or features is None:
            raise RuntimeError("clip features must exist before deep analysis")
        run.stage = "deep_video"
        storage_key = clip.storage_key
        extension = clip.extension or "mp4"
        transcript = features.transcript

    work_dir = settings.work_dir / "deep-video" / run_id
    shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    raw_path = work_dir / f"input.{extension}"
    try:
        ObjectStore().download_file(storage_key, raw_path)
        result = analyze_full_video(
            raw_path,
            transcript,
            reference_id=run_id,
            settings=settings,
        )
        payload = result.value.model_dump(mode="json")
        with session_scope() as session:
            run = session.get(ClipAnalysisRun, run_uuid)
            features = session.get(ClipFeature, run.clip_id) if run is not None else None
            if run is None or features is None:
                raise RuntimeError("analysis state disappeared during deep video analysis")
            ai_features = dict(features.ai_features or {})
            ai_features["deep"] = payload
            ai_features["deep_provider"] = result.target.provider
            ai_features["deep_model"] = result.target.model
            features.ai_features = ai_features
        return {"provider": result.target.provider, "model": result.target.model}
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


@activity.defn
def score_local_candidate(run_id: str) -> dict[str, object]:
    run_uuid = uuid.UUID(run_id)
    with session_scope() as session:
        run = session.get(ClipAnalysisRun, run_uuid)
        if run is None:
            raise ValueError(f"analysis run not found: {run_id}")
        clip = session.get(Clip, run.clip_id)
        if clip is None:
            raise ValueError(f"clip not found: {run.clip_id}")
        features = session.get(ClipFeature, clip.id)
        if features is None:
            raise RuntimeError("local features must exist before scoring")
        run.stage = "scoring"
        source_metrics = _merge_source_metrics(clip)
        result = score_candidate(
            local_features=dict(features.local_features or {}),
            source_metrics=source_metrics,
            ai_features=dict(features.ai_features or {}),
        )
        features.candidate_score = Decimal(str(result.score))
        features.score_breakdown = result.breakdown
        if ClipStatus(clip.status) == ClipStatus.ANALYZED:
            require_clip_transition(ClipStatus.ANALYZED, ClipStatus.SCORED)
            clip.status = ClipStatus.SCORED.value
        run.status = AnalysisStatus.COMPLETED.value
        run.stage = "completed"
        run.completed_at = datetime.now(UTC)
        run.error = None
        session.add(
            DomainEvent(
                aggregate_type="clip",
                aggregate_id=str(clip.id),
                event_type="clip.scored",
                payload={
                    "clip_id": str(clip.id),
                    "analysis_run_id": run_id,
                    "candidate_score": result.score,
                    "score_breakdown": result.breakdown,
                },
            )
        )
        return {
            "run_id": run_id,
            "clip_id": str(clip.id),
            "candidate_score": result.score,
        }


@activity.defn
def mark_analysis_failed(run_id: str, message: str) -> None:
    run_uuid = uuid.UUID(run_id)
    with session_scope() as session:
        run = session.get(ClipAnalysisRun, run_uuid)
        if run is None:
            return
        run.status = AnalysisStatus.FAILED.value
        run.stage = "failed"
        run.error = message[:8000]
        session.add(
            DomainEvent(
                aggregate_type="analysis_run",
                aggregate_id=str(run.id),
                event_type="clip.analysis_failed",
                payload={
                    "analysis_run_id": str(run.id),
                    "clip_id": str(run.clip_id),
                    "error": run.error,
                },
            )
        )
