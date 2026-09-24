from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from temporalio import activity
from temporalio.exceptions import ApplicationError

from katcha.acquisition.adapters import DiscoveryProviderError, get_adapter
from katcha.acquisition_models import DiscoveryRun
from katcha.config import get_settings
from katcha.db import session_scope
from katcha.domain import DiscoveryRunStatus
from katcha.models import DomainEvent
from katcha.services.discovery import observe_discovery_candidate
from katcha.services.discovery_polling import (
    mark_poll_provider_started,
    mark_quota_exhausted,
    page_identity,
    poll_attempt_for_run,
    record_poll_failure,
    record_poll_page_success,
    reserve_provider_page,
)
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


def _poll_attempt_id(metadata: dict[str, object]) -> uuid.UUID | None:
    raw = metadata.get("poll_attempt_id")
    if not raw:
        return None
    try:
        return uuid.UUID(str(raw))
    except ValueError as exc:
        raise ValueError("discovery run has an invalid poll_attempt_id") from exc


@activity.defn
def execute_discovery_page_activity(run_id: str) -> dict[str, object]:
    run_uuid = uuid.UUID(run_id)
    with session_scope() as session:
        run = session.get(DiscoveryRun, run_uuid)
        if run is None:
            raise ValueError(f"discovery run not found: {run_id}")
        run_metadata = dict(run.run_metadata or {})
        managed_attempt_id = _poll_attempt_id(run_metadata)
        if run.status == DiscoveryRunStatus.COMPLETED.value:
            if managed_attempt_id is None:
                record_discovery_run_success(run_uuid)
            return {
                "run_id": run_id,
                "candidate_count": 0,
                "done": True,
                "reused": True,
            }
        if run.status == DiscoveryRunStatus.FAILED.value:
            raise ApplicationError(
                "failed discovery run must be explicitly retried",
                non_retryable=True,
            )
        if run.status == DiscoveryRunStatus.QUEUED.value:
            run.status = DiscoveryRunStatus.RUNNING.value
            run.started_at = datetime.now(UTC)
            run.error = None
        adapter_key = run.adapter_key
        adapter_version = run.adapter_version
        query = dict(run.query or {})
        cursor = dict(run.cursor or {})

    topic_watch_id: uuid.UUID | None = None
    raw_topic_watch_id = run_metadata.get("topic_watch_id")
    if raw_topic_watch_id:
        try:
            topic_watch_id = uuid.UUID(str(raw_topic_watch_id))
        except ValueError as exc:
            raise ValueError("discovery run has an invalid topic_watch_id") from exc

    page_key = page_identity(cursor)
    if managed_attempt_id is not None:
        raw_config = run_metadata.get("poll_adapter_config")
        adapter_config = dict(raw_config) if isinstance(raw_config, dict) else {}
        quota = reserve_provider_page(
            managed_attempt_id,
            adapter_key=adapter_key,
            adapter_config=adapter_config,
            cursor=cursor,
        )
        page_key = quota.page_key
        if not quota.allowed:
            mark_quota_exhausted(
                managed_attempt_id,
                discovery_run_id=run_uuid,
                page_key=page_key,
                reason=quota.reason,
                blocked_until=quota.blocked_until,
            )
            raise ApplicationError(
                f"discovery provider quota unavailable: {quota.reason}",
                non_retryable=True,
            )
        mark_poll_provider_started(managed_attempt_id)

    adapter = get_adapter(adapter_key, adapter_version)
    try:
        batch = adapter.discover(query, cursor)
    except DiscoveryProviderError as exc:
        if managed_attempt_id is not None:
            record_poll_failure(
                managed_attempt_id,
                discovery_run_id=run_uuid,
                page_key=page_key,
                kind=exc.kind,
                transient=exc.transient,
                status_code=exc.status_code,
                retry_after_seconds=exc.retry_after_seconds,
                message=str(exc),
                consume_reserved=True,
                provider_usage=(
                    dict(exc.provider_usage) if exc.provider_usage else None
                ),
            )
            raise ApplicationError(str(exc), non_retryable=True) from exc
        raise
    except ValueError as exc:
        if managed_attempt_id is not None:
            record_poll_failure(
                managed_attempt_id,
                discovery_run_id=run_uuid,
                page_key=page_key,
                kind="configuration_or_payload",
                transient=False,
                message=str(exc),
                consume_reserved=False,
            )
            raise ApplicationError(str(exc), non_retryable=True) from exc
        raise
    except Exception as exc:
        if managed_attempt_id is not None:
            record_poll_failure(
                managed_attempt_id,
                discovery_run_id=run_uuid,
                page_key=page_key,
                kind="unexpected_provider_error",
                transient=True,
                message="discovery provider failed unexpectedly",
                consume_reserved=True,
            )
            raise ApplicationError(
                "discovery provider failed unexpectedly",
                non_retryable=True,
            ) from exc
        raise

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

    if managed_attempt_id is not None:
        poll_outcome = record_poll_page_success(
            managed_attempt_id,
            discovery_run_id=run_uuid,
            candidate_count=len(candidate_ids),
            next_cursor=dict(batch.next_cursor or {}),
            done=batch.done,
            provider_usage=dict(batch.provider_usage or {}),
            page_key=page_key,
        )
        return {
            "run_id": run_id,
            "candidate_count": len(candidate_ids),
            "done": batch.done,
            "reused": False,
            "poll_outcome": poll_outcome,
            "provider_usage": dict(batch.provider_usage or {}),
        }

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
    if completed:
        record_discovery_run_success(run_uuid)
    return {
        "run_id": run_id,
        "candidate_count": len(candidate_ids),
        "done": batch.done,
        "reused": False,
        "provider_usage": dict(batch.provider_usage or {}),
    }


@activity.defn
def mark_discovery_run_failed(run_id: str, message: str) -> None:
    run_uuid = uuid.UUID(run_id)
    managed = poll_attempt_for_run(run_uuid)
    if managed is not None:
        with session_scope() as session:
            run = session.get(DiscoveryRun, run_uuid)
            if run is None:
                return
            cursor = dict(run.cursor or {})
        record_poll_failure(
            managed.id,
            discovery_run_id=run_uuid,
            page_key=page_identity(cursor),
            kind="workflow_or_persistence_failure",
            transient=True,
            message="discovery workflow failed after provider execution",
            consume_reserved=True,
        )
        return

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
                "run_id": str(run.run_id) if run.run_id is not None else None,
                "adapter_key": run.adapter_key,
                "adapter_version": run.adapter_version,
                "workflow_id": run.workflow_id,
                "poll_attempt_id": str(run.poll_attempt_id),
                "source_state_id": str(run.source_state_id),
                "action": run.action,
                "reason": run.reason,
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
