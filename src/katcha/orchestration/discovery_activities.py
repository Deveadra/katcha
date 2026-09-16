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
