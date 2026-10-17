from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from temporalio import activity

from katcha.acquisition_models import DiscoveryRun
from katcha.config import get_ai_settings as get_settings
from katcha.db import session_scope
from katcha.domain import DiscoveryRunStatus
from katcha.orchestration.discovery_activities import bridge_source_opportunities_activity
from katcha.services.research import prepare_research_jobs

logger = logging.getLogger(__name__)


def reconcile_completed_source_handoffs(
    *, now: datetime | None = None, limit: int = 2
) -> dict[str, object]:
    """Backfill previously completed channel Source checks without provider requests.

    Only old, channel-scoped, unprocessed source runs are eligible. Existing
    topic-watch jobs already bridge through their queue and are excluded.
    """
    if not get_settings().research_enabled:
        return {"status": "disabled", "processed": 0, "failures": 0}
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    cutoff = now - timedelta(minutes=10)
    bounded = max(1, min(int(limit), 5))
    metadata = DiscoveryRun.run_metadata
    with session_scope() as session:
        ids = list(
            session.scalars(
                select(DiscoveryRun.id)
                .where(
                    DiscoveryRun.status == DiscoveryRunStatus.COMPLETED.value,
                    DiscoveryRun.completed_at.is_not(None),
                    DiscoveryRun.completed_at <= cutoff,
                    metadata["ingestion_source_id"].as_string().is_not(None),
                    metadata["source_scope"].as_string() == "channel",
                    metadata["topic_watch_id"].as_string().is_(None),
                    metadata["opportunity_handoff"]["status"].as_string().is_(None),
                )
                .order_by(DiscoveryRun.completed_at, DiscoveryRun.id)
                .limit(bounded)
            )
        )

    statuses: dict[str, int] = {}
    failures = 0
    for run_id in ids:
        try:
            result = bridge_source_opportunities_activity(str(run_id))
            status = str(result.get("status") or "unknown")
            statuses[status] = statuses.get(status, 0) + 1
            if status == "failed":
                failures += 1
        except Exception:
            failures += 1
            logger.exception("Historical Source dossier reconciliation failed for %s", run_id)
    return {
        "status": "completed" if not failures else "partial_failure",
        "processed": len(ids),
        "failures": failures,
        "outcomes": statuses,
    }


@activity.defn
def prepare_research_jobs_activity(now: str) -> list[dict]:
    timestamp = datetime.fromisoformat(now)
    jobs = prepare_research_jobs(timestamp)
    # Use the *existing* activity boundary, not a modification to the live
    # Temporal workflow history. Work is data-only and capped per tick.
    # The guard prevents additional scans on each one-minute scheduler cycle.
    if timestamp.minute % 5 == 0:
        try:
            summary = reconcile_completed_source_handoffs(now=timestamp, limit=2)
            if summary["processed"]:
                logger.info("Historical Source dossier reconciliation: %s", summary)
        except Exception:
            # A stale stored Source must never interrupt live research dispatch.
            logger.exception("Source dossier reconciliation unavailable")
    return jobs
