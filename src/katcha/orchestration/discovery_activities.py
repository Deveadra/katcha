from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from temporalio import activity

from katcha.acquisition.adapters import get_adapter
from katcha.acquisition_models import DiscoveryRun
from katcha.db import session_scope
from katcha.domain import DiscoveryRunStatus
from katcha.models import DomainEvent
from katcha.services.discovery import observe_discovery_candidate
from katcha.services.trend_execution import prepare_topic_watch_execution
from katcha.services.trend_queue import materialize_trend_review_queue
from katcha.services.trends import compute_candidate_trend_score


@activity.defn
def execute_discovery_page_activity(run_id: str) -> dict[str, object]:
    run_uuid = uuid.UUID(run_id)
    with session_scope() as session:
        run = session.get(DiscoveryRun, run_uuid)
        if run is None:
            raise ValueError(f"discovery run not found: {run_id}")
        if run.status == DiscoveryRunStatus.COMPLETED.value:
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

    with session_scope() as session:
        run = session.scalar(select(DiscoveryRun).where(DiscoveryRun.id == run_uuid))
        if run is None:
            raise RuntimeError("discovery run disappeared during execution")
        run.cursor = dict(batch.next_cursor or {})
        if batch.done:
            run.status = DiscoveryRunStatus.COMPLETED.value
            run.completed_at = datetime.now(UTC)
        else:
            run.status = DiscoveryRunStatus.RUNNING.value
        session.add(
            DomainEvent(
                aggregate_type="discovery_run",
                aggregate_id=run_id,
                event_type=(
                    "discovery_run.completed"
                    if batch.done
                    else "discovery_run.page_completed"
                ),
                payload={
                    "discovery_run_id": run_id,
                    "adapter_key": adapter_key,
                    "adapter_version": adapter_version,
                    "candidate_count": len(candidate_ids),
                    "done": batch.done,
                    "topic_watch_id": (
                        str(topic_watch_id) if topic_watch_id is not None else None
                    ),
                },
            )
        )
    return {
        "run_id": run_id,
        "candidate_count": len(candidate_ids),
        "done": batch.done,
        "reused": False,
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


@activity.defn
def prepare_topic_watch_execution_activity(
    topic_watch_id: str,
    execution_key: str,
) -> dict[str, object]:
    runs = prepare_topic_watch_execution(
        uuid.UUID(topic_watch_id),
        execution_key=execution_key,
    )
    return {
        "topic_watch_id": topic_watch_id,
        "execution_key": execution_key,
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
    result = materialize_trend_review_queue(
        uuid.UUID(topic_watch_id),
        queue_key=execution_key,
        discovery_run_ids=[uuid.UUID(run_id) for run_id in discovery_run_ids],
        top_n=top_n,
    )
    return {
        "topic_watch_id": str(result.topic_watch_id),
        "queue_key": result.queue_key,
        "candidate_count": result.candidate_count,
        "cluster_count": result.cluster_count,
        "queue_count": result.queue_count,
        "reused": result.reused,
    }
