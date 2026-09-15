from __future__ import annotations

import uuid

from sqlalchemy import select
from temporalio import activity

from katcha.db import session_scope
from katcha.models import ClipAnalysisRun, ClipFeature, DomainEvent
from katcha.services.similarity import rank_similar_hashes


@activity.defn
def detect_near_duplicates(run_id: str) -> dict[str, object]:
    run_uuid = uuid.UUID(run_id)
    with session_scope() as session:
        run = session.get(ClipAnalysisRun, run_uuid)
        if run is None:
            raise ValueError(f"analysis run not found: {run_id}")
        features = session.get(ClipFeature, run.clip_id)
        if features is None:
            raise RuntimeError("clip features must exist before similarity matching")
        run.stage = "similarity"
        rows = session.execute(
            select(ClipFeature.clip_id, ClipFeature.perceptual_hashes).where(
                ClipFeature.clip_id != run.clip_id
            )
        ).all()
        candidates = [(str(clip_id), list(hashes or [])) for clip_id, hashes in rows]
        matches = rank_similar_hashes(list(features.perceptual_hashes or []), candidates)
        local_features = dict(features.local_features or {})
        local_features["near_duplicates"] = [
            {"clip_id": match.clip_id, "mean_hash_distance": match.mean_distance}
            for match in matches
        ]
        features.local_features = local_features
        session.add(
            DomainEvent(
                aggregate_type="clip",
                aggregate_id=str(run.clip_id),
                event_type="clip.similarity_checked",
                payload={
                    "clip_id": str(run.clip_id),
                    "analysis_run_id": run_id,
                    "near_duplicates": local_features["near_duplicates"],
                },
            )
        )
        return {
            "match_count": len(matches),
            "best_distance": matches[0].mean_distance if matches else None,
        }
