from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from temporalio import activity

from katcha.acquisition.adapters import get_adapter
from katcha.acquisition_models import DiscoveryRun
from katcha.config import get_settings
from katcha.db import session_scope
from katcha.domain import DiscoveryRunStatus
from katcha.models import DomainEvent
from katcha.services.discovery import observe_discovery_candidate
from katcha.services.discovery_quota import reserve_youtube_page
from katcha.services.discovery_trends import compute_candidate_trend_score
from katcha.services.trend_execution import prepare_topic_watch_execution
from katcha.services.trend_queue import materialize_trend_review_queue
from katcha.services.trend_signal_bridge import bridge_topic_watch_queue_to_trend_signals
from katcha.services.trend_source_reliability import (
    record_discovery_run_failure,
    record_discovery_run_success,
    source_health_allows_refresh,
    topic_watch_source_health,
)


@activity.defn
def execute_discovery_page_activity(run_id: str) -> dict[str, object]:
    run_uuid = uuid.UUID(run_id)
    with session_scope() as session:
        run = session.get(DiscoveryRun, run_uuid)
        if run is None:
            raise ValueError(f"discovery run not found: {run_id}")
        if run.status == DiscoveryRunStatus.COMPLETED.value:
            record_discovery_run_success(run_uuid)
            return {
                "run_id": run_id,
                "candidate_count": 0,
                "done": True,
                "reused": True,
            }
        if run.status == DiscoveryRunStatus.FAILED.value:
            raise ValueError("failed discovery run must be explicitly retried")
        if run.status == DiscoveryRunStatus.QUEUED.value:
            run.status = DiscoveryRunStatus.RUNNING.value
            run.started_at = datetime.now(UTC)
            run.error = None
        adapter_key = run.adapter_key
        adapter_version = run.adapter_version
        query = dict(run.query or {})
        cursor = dict(run.cursor or {})
        run_metadata = dict(run.run_metadata or {})

    topic_watch_id: uuid.UUID | None = None
    raw_topic_watch_id = run_metadata.get("topic_watch_id")
    if raw_topic_watch_id:
        try:
            topic_watch_id = uuid.UUID(str(raw_topic_watch_id))
        except ValueError as exc:
            raise ValueError("discovery run has an invalid topic_watch_id") from exc

    quota_estimate = reserve_youtube_page(run_uuid) if adapter_key == "youtube" else None
    adapter = get_adapter(adapter_key, adapter_version)
    batch = adapter.discover(query, cursor)
    candidate_ids: list[str] = []
    for item in batch.items:
        candidate = observe_discovery_candidate(
            source_url=item.source_url,
            adapter_key=adapter_key,
            discovery_run_id=run_uuid,
            external_id=item.external_id,
            title=item.title,
            creator=item.creator,
            creator_url=item.creator_url,
            provenance_confidence=item.provenance_confidence,
            provenance_claims=item.provenance_claims,
            metadata=item.metadata,
        )
        candidate_ids.append(str(candidate.id))
        if topic_watch_id is not None:
            compute_candidate_trend_score(
                candidate.id,
                topic_watch_id,
                score_key=f"run:{run_uuid}",
            )

    completed = False
    with session_scope() as session:
        run = session.scalar(select(DiscoveryRun).where(DiscoveryRun.id == run_uuid))
        if run is None:
            raise RuntimeError("discovery run disappeared during execution")
        run.cursor = dict(batch.next_cursor or {})
        if batch.done:
            run.status = DiscoveryRunStatus.COMPLETED.value
            run.completed_at = datetime.now(UTC)
            completed = True
        else:
            run.status = DiscoveryRunStatus.RUNNING.value
        session.add(
            DomainEvent(
                aggregate_type="discovery_run",
                aggregate_id=run_id,
                event_type=(
                    "discovery_run.completed" if batch.done else "discovery_run.page_completed"
                ),
                payload={
                    "discovery_run_id": run_id,
                    "adapter_key": adapter_key,
                    "adapter_version": adapter_version,
                    "candidate_count": len(candidate_ids),
                    "done": batch.done,
                    "quota_estimate": quota_estimate,
                    "topic_watch_id": (str(topic_watch_id) if topic_watch_id is not None else None),
                },
            )
        )
    if completed:
        record_discovery_run_success(run_uuid)
    return {
        "run_id": run_id,
        "candidate_count": len(candidate_ids),
        "done": batch.done,
        "reused": False,
        "quota_estimate": quota_estimate,
    }


@activity.defn
def mark_discovery_run_failed(run_id: str, message: str) -> None:
    run_uuid = uuid.UUID(run_id)
    with session_scope() as session:
        run = session.get(DiscoveryRun, run_uuid)
        if run is None:
            return
        run.status = DiscoveryRunStatus.FAILED.value
        run.error = message[:8000]
        run.completed_at = datetime.now(UTC)
        session.add(
            DomainEvent(
                aggregate_type="discovery_run",
                aggregate_id=run_id,
                event_type="discovery_run.failed",
                payload={
                    "discovery_run_id": run_id,
                    "error": message[:2000],
                },
            )
        )
    record_discovery_run_failure(run_uuid, message)


@activity.defn
def prepare_topic_watch_execution_activity(
    topic_watch_id: str,
    execution_key: str,
) -> dict[str, object]:
    watch_uuid = uuid.UUID(topic_watch_id)
    runs = prepare_topic_watch_execution(
        watch_uuid,
        execution_key=execution_key,
    )
    return {
        "topic_watch_id": topic_watch_id,
        "execution_key": execution_key,
        "source_health": topic_watch_source_health(watch_uuid),
        "runs": [
            {
                "run_id": str(run.run_id),
                "adapter_key": run.adapter_key,
                "adapter_version": run.adapter_version,
                "workflow_id": run.workflow_id,
            }
            for run in runs
        ],
    }


@activity.defn
def finalize_topic_watch_execution_activity(
    topic_watch_id: str,
    execution_key: str,
    discovery_run_ids: list[str],
    top_n: int,
) -> dict[str, object]:
    watch_uuid = uuid.UUID(topic_watch_id)
    health = topic_watch_source_health(watch_uuid)
    settings = get_settings()
    if not source_health_allows_refresh(
        health,
        minimum_coverage=settings.trend_min_source_coverage,
    ):
        return {
            "topic_watch_id": topic_watch_id,
            "queue_key": execution_key,
            "status": "withheld_source_coverage",
            "source_health": health,
            "minimum_source_coverage": settings.trend_min_source_coverage,
            "candidate_count": 0,
            "cluster_count": 0,
            "queue_count": 0,
            "signals_bridged": 0,
            "channel_profile_id": None,
        }
    result = materialize_trend_review_queue(
        watch_uuid,
        queue_key=execution_key,
        discovery_run_ids=[uuid.UUID(run_id) for run_id in discovery_run_ids],
        top_n=top_n,
    )
    bridge = bridge_topic_watch_queue_to_trend_signals(
        watch_uuid,
        queue_key=execution_key,
    )
    return {
        "topic_watch_id": str(result.topic_watch_id),
        "queue_key": result.queue_key,
        "status": "completed",
        "source_health": health,
        "candidate_count": result.candidate_count,
        "cluster_count": result.cluster_count,
        "queue_count": result.queue_count,
        "reused": result.reused,
        **bridge,
    }
