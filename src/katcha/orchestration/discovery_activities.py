from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from temporalio import activity
from temporalio.exceptions import ApplicationError

from katcha.acquisition.adapters import get_adapter
from katcha.acquisition.errors import DiscoveryProviderError, ProviderFailure
from katcha.acquisition_models import DiscoveryRun
from katcha.db import session_scope
from katcha.domain import DiscoveryRunStatus
from katcha.models import DomainEvent
from katcha.services.discovery import observe_discovery_candidate
from katcha.services.discovery_health import (
    record_source_failure,
    record_source_page_success,
)
from katcha.services.discovery_trends import compute_candidate_trend_score
from katcha.services.trend_execution import prepare_topic_watch_execution
from katcha.services.trend_queue import materialize_trend_review_queue


def _source_lineage(
    metadata: dict[str, object],
) -> tuple[uuid.UUID | None, str | None]:
    raw_state_id = metadata.get("source_state_id")
    attempt_key = str(metadata.get("source_attempt_key") or "").strip() or None
    if not raw_state_id:
        return None, attempt_key
    try:
        return uuid.UUID(str(raw_state_id)), attempt_key
    except ValueError as exc:
        raise ValueError("discovery run has an invalid source_state_id") from exc


def _mark_failed_run(
    run_id: uuid.UUID,
    *,
    failure: ProviderFailure,
) -> None:
    with session_scope() as session:
        run = session.get(DiscoveryRun, run_id)
        if run is None:
            return
        if run.status != DiscoveryRunStatus.COMPLETED.value:
            run.status = DiscoveryRunStatus.FAILED.value
            run.error = (
                f"{failure.provider} {failure.operation}: {failure.kind}"
                + (
                    f" status={failure.status_code}"
                    if failure.status_code is not None
                    else ""
                )
            )[:8000]
            run.completed_at = datetime.now(UTC)
        session.add(
            DomainEvent(
                aggregate_type="discovery_run",
                aggregate_id=str(run_id),
                event_type="discovery_run.failed",
                payload={
                    "discovery_run_id": str(run_id),
                    "adapter_key": run.adapter_key,
                    "error_kind": failure.kind,
                    "http_status": failure.status_code,
                    "retryable": failure.retryable,
                },
            )
        )


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
        run_metadata = dict(run.run_metadata or {})

    topic_watch_id: uuid.UUID | None = None
    raw_topic_watch_id = run_metadata.get("topic_watch_id")
    if raw_topic_watch_id:
        try:
            topic_watch_id = uuid.UUID(str(raw_topic_watch_id))
        except ValueError as exc:
            raise ValueError("discovery run has an invalid topic_watch_id") from exc
    source_state_id, attempt_key = _source_lineage(run_metadata)

    adapter = get_adapter(adapter_key, adapter_version)
    try:
        batch = adapter.discover(query, cursor)
    except DiscoveryProviderError as exc:
        failure = exc.failure
        if source_state_id is not None and attempt_key is not None:
            record_source_failure(
                source_state_id,
                attempt_key=attempt_key,
                discovery_run_id=run_uuid,
                failure=failure,
            )
        _mark_failed_run(run_uuid, failure=failure)
        raise ApplicationError(str(exc), non_retryable=True) from exc
    except ValueError as exc:
        failure = ProviderFailure(
            provider=adapter_key,
            operation="discover",
            kind="configuration_or_payload",
            retryable=False,
        )
        if source_state_id is not None and attempt_key is not None:
            record_source_failure(
                source_state_id,
                attempt_key=attempt_key,
                discovery_run_id=run_uuid,
                failure=failure,
            )
        _mark_failed_run(run_uuid, failure=failure)
        raise ApplicationError(str(exc), non_retryable=True) from exc
    except Exception as exc:
        failure = ProviderFailure(
            provider=adapter_key,
            operation="discover",
            kind="unexpected_provider_error",
            retryable=True,
        )
        if source_state_id is not None and attempt_key is not None:
            record_source_failure(
                source_state_id,
                attempt_key=attempt_key,
                discovery_run_id=run_uuid,
                failure=failure,
            )
        _mark_failed_run(run_uuid, failure=failure)
        raise ApplicationError(
            "discovery provider failed unexpectedly", non_retryable=True
        ) from exc

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

    next_cursor = dict(batch.next_cursor or {})
    with session_scope() as session:
        run = session.scalar(select(DiscoveryRun).where(DiscoveryRun.id == run_uuid))
        if run is None:
            raise RuntimeError("discovery run disappeared during execution")
        run.cursor = next_cursor
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
                    "source_state_id": (
                        str(source_state_id) if source_state_id is not None else None
                    ),
                },
            )
        )

    poll_outcome = None
    if source_state_id is not None and attempt_key is not None:
        poll_outcome = record_source_page_success(
            source_state_id,
            attempt_key=attempt_key,
            discovery_run_id=run_uuid,
            candidate_count=len(candidate_ids),
            next_cursor=next_cursor,
            done=batch.done,
        )
    return {
        "run_id": run_id,
        "candidate_count": len(candidate_ids),
        "done": batch.done,
        "reused": False,
        "poll_outcome": poll_outcome,
    }


@activity.defn
def mark_discovery_run_failed(run_id: str, message: str) -> None:
    run_uuid = uuid.UUID(run_id)
    with session_scope() as session:
        run = session.get(DiscoveryRun, run_uuid)
        if run is None:
            return
        metadata = dict(run.run_metadata or {})
        adapter_key = run.adapter_key
        if run.status != DiscoveryRunStatus.COMPLETED.value:
            run.status = DiscoveryRunStatus.FAILED.value
            run.error = message[:8000]
            run.completed_at = datetime.now(UTC)
    source_state_id, attempt_key = _source_lineage(metadata)
    if source_state_id is not None and attempt_key is not None:
        record_source_failure(
            source_state_id,
            attempt_key=attempt_key,
            discovery_run_id=run_uuid,
            failure=ProviderFailure(
                provider=adapter_key,
                operation="discovery_run",
                kind="workflow_failure",
                retryable=True,
            ),
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
                "run_id": str(run.run_id) if run.run_id is not None else None,
                "adapter_key": run.adapter_key,
                "adapter_version": run.adapter_version,
                "workflow_id": run.workflow_id,
                "source_state_id": str(run.source_state_id),
                "source_key": run.source_key,
                "attempt_key": run.attempt_key,
                "allowed": run.allowed,
                "reason": run.reason,
                "next_eligible_poll_at": run.next_eligible_poll_at,
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
