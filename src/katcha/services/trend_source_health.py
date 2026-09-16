from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from katcha.db import session_scope
from katcha.trend_source_models import TrendSourceSubscription


def source_health_summary(channel_profile_id: uuid.UUID) -> dict[str, Any]:
    with session_scope() as session:
        sources = list(
            session.scalars(
                select(TrendSourceSubscription).where(
                    TrendSourceSubscription.channel_profile_id == channel_profile_id,
                    TrendSourceSubscription.status == "active",
                )
            )
        )
    if not sources:
        return {
            "configured": 0,
            "healthy": 0,
            "degraded": 0,
            "unknown": 0,
            "coverage": 1.0,
        }
    healthy = sum(1 for source in sources if source.health_status == "healthy")
    unknown = sum(1 for source in sources if source.health_status == "unknown")
    degraded = len(sources) - healthy - unknown
    coverage = (healthy + 0.5 * unknown) / len(sources)
    return {
        "configured": len(sources),
        "healthy": healthy,
        "degraded": degraded,
        "unknown": unknown,
        "coverage": round(coverage, 6),
    }
