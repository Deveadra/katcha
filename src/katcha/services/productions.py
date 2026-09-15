from __future__ import annotations

import hashlib
import uuid
from decimal import Decimal

from sqlalchemy import select

from katcha.db import session_scope
from katcha.domain import ProductionStatus
from katcha.editorial.personas import get_persona
from katcha.models import Clip, ClipFeature
from katcha.production_models import Production

PROMPT_VERSION = "short-script-v1"


def _workflow_id(clip_id: uuid.UUID, idempotency_key: str | None) -> str:
    if idempotency_key:
        digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:20]
        return f"short-prod-{clip_id}-{digest}"
    return f"short-prod-{clip_id}-{uuid.uuid4().hex[:20]}"


def register_short_production(
    clip_id: uuid.UUID,
    *,
    persona_key: str = "youth_host",
    idempotency_key: str | None = None,
) -> Production:
    persona = get_persona(persona_key)
    workflow_id = _workflow_id(clip_id, idempotency_key)

    with session_scope() as session:
        existing = session.scalar(
            select(Production).where(Production.workflow_id == workflow_id)
        )
        if existing is not None:
            return existing

        clip = session.get(Clip, clip_id)
        features = session.get(ClipFeature, clip_id)
        if clip is None:
            raise ValueError(f"clip not found: {clip_id}")
        if features is None or features.candidate_score is None:
            raise ValueError("clip must have completed scoring before production")

        snapshot = {
            "clip_sha256": clip.sha256,
            "duration_seconds": float(clip.duration_seconds or 0),
            "transcript": features.transcript,
            "local_features": dict(features.local_features or {}),
            "ai_features": dict(features.ai_features or {}),
            "candidate_score": float(features.candidate_score),
            "score_breakdown": dict(features.score_breakdown or {}),
            "features_updated_at": features.updated_at.isoformat() if features.updated_at else None,
        }
        production = Production(
            clip_id=clip_id,
            workflow_id=workflow_id,
            kind="short",
            status=ProductionStatus.QUEUED.value,
            stage="queued",
            persona_key=persona.key,
            persona_version=persona.version,
            prompt_version=PROMPT_VERSION,
            analysis_snapshot=snapshot,
            estimated_cost_usd=Decimal("0"),
        )
        session.add(production)
        session.flush()
        session.refresh(production)
        session.expunge(production)
        return production
