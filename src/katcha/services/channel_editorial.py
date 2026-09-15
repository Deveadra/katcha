from __future__ import annotations

import uuid

from katcha.config import get_settings
from katcha.db import session_scope
from katcha.intelligence.features import clip_learning_features
from katcha.longform.candidates import select_compilation_candidates
from katcha.longform_models import Compilation
from katcha.models import Clip, ClipFeature, DomainEvent
from katcha.services.channel_learning import score_channel_features
from katcha.services.channel_profiles import ensure_active_profile


def score_clip_for_channel(
    channel_profile_id: uuid.UUID,
    clip_id: uuid.UUID,
) -> dict[str, object]:
    with session_scope() as session:
        ensure_active_profile(session, channel_profile_id)
        clip = session.get(Clip, clip_id)
        features = session.get(ClipFeature, clip_id)
        if clip is None:
            raise ValueError(f"clip not found: {clip_id}")
        if features is None or features.candidate_score is None:
            raise ValueError("clip must have completed scoring")
        vector = clip_learning_features(clip, features)
    score, details = score_channel_features(channel_profile_id, vector)
    return {
        "channel_profile_id": str(channel_profile_id),
        "clip_id": str(clip_id),
        "score": score,
        "features": vector,
        "details": details,
    }


def freeze_channel_compilation_candidates(
    compilation_id: uuid.UUID,
) -> Compilation:
    settings = get_settings()
    with session_scope() as session:
        compilation = session.get(Compilation, compilation_id)
        if compilation is None:
            raise ValueError(f"compilation not found: {compilation_id}")
        if compilation.channel_profile_id is None:
            raise ValueError("compilation is not channel-scoped")
        ensure_active_profile(session, compilation.channel_profile_id)
        if (compilation.candidate_snapshot or {}).get("candidates"):
            session.expunge(compilation)
            return compilation

        candidates = select_compilation_candidates(
            session,
            theme=compilation.theme,
            target_duration_seconds=compilation.target_duration_seconds,
            target_segment_count=compilation.target_segment_count,
            settings=settings,
            channel_profile_id=compilation.channel_profile_id,
        )
        payload = [item.model_dump(mode="json") for item in candidates]
        measured = sum(bool(item.evidence.get("measured_short")) for item in candidates)
        compilation.candidate_snapshot = {
            "version": "candidate-snapshot-v2-channel",
            "candidates": payload,
            "measured_candidate_count": measured,
            "selection_policy": "channel-performance-diversity-learning-v1",
            "channel_profile_id": str(compilation.channel_profile_id),
        }
        compilation.stage = "candidates_prefrozen"
        session.add(
            DomainEvent(
                aggregate_type="compilation",
                aggregate_id=str(compilation.id),
                event_type="compilation.candidates_prefrozen",
                payload={
                    "compilation_id": str(compilation.id),
                    "channel_profile_id": str(compilation.channel_profile_id),
                    "candidate_count": len(candidates),
                    "measured_candidate_count": measured,
                    "selection_policy": "channel-performance-diversity-learning-v1",
                },
            )
        )
        session.flush()
        session.refresh(compilation)
        session.expunge(compilation)
        return compilation
