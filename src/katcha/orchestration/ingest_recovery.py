"""Temporal-evidenced recovery guard for persisted in-progress media ingests.

A process restart is not proof that an unfinished R2 upload is safe to repeat.
Never re-dispatch an INGESTING SourceItem from worker startup. First inspect the
recorded Temporal execution and persist a diagnostic for non-running outcomes.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from temporalio.client import RPCError, RPCStatusCode, WorkflowExecutionStatus

from katcha.db import session_scope
from katcha.domain import SourceStatus
from katcha.models import DomainEvent, SourceItem

logger = logging.getLogger(__name__)

TEMPORAL_DESCRIBE_TIMEOUT_SECONDS = 8.0
RECOVERY_KEY = "ingest_recovery"
_RECOVERY_TEXT = (
    "Inspect this SourceItem, its Temporal execution history, R2 staging and "
    "content-addressed Clip storage before any new ingest attempt. "
    "No automatic replay was performed."
)


def _persist_ingest_diagnostic(
    source_id: uuid.UUID, workflow_id: str, diagnostic: dict[str, object] | None,
) -> bool:
    """Idempotent, source- and workflow-fenced diagnostic with audit evidence."""
    with session_scope() as session:
        source = session.scalar(
            select(SourceItem).where(SourceItem.id == source_id).with_for_update()
        )
        if (
            source is None
            or source.status != SourceStatus.INGESTING.value
            or source.workflow_id != workflow_id
        ):
            # A concurrently completed ingest must not be relabeled as blocked.
            return False
        metadata = dict(source.source_metadata or {})
        previous = metadata.get(RECOVERY_KEY)
        if diagnostic is None:
            if previous is None:
                return False
            metadata.pop(RECOVERY_KEY, None)
            event_type = "source.ingest_execution_observed_running"
            event_payload = {
                "workflow_id": workflow_id,
                "previous_code": (
                    previous.get("code")
                    if isinstance(previous, dict)
                    and isinstance(previous.get("code"), str)
                    and previous.get("code") in {
                        "ingest_temporal_not_running",
                        "ingest_temporal_execution_missing",
                        "ingest_temporal_visibility_unavailable",
                    }
                    else "unknown"
                ),
            }
        else:
            if isinstance(previous, dict) and all(
                previous.get(key) == value for key, value in diagnostic.items()
            ):
                return False
            metadata[RECOVERY_KEY] = {
                **diagnostic,
                "observed_at": datetime.now(UTC).isoformat(),
            }
            event_type = "source.ingest_recovery_required"
            event_payload = {
                "workflow_id": workflow_id,
                "code": diagnostic["code"],
                "workflow_status": diagnostic.get("workflow_status"),
            }
        source.source_metadata = metadata
        session.add(DomainEvent(
            aggregate_type="source",
            aggregate_id=str(source.id),
            event_type=event_type,
            payload={"source_id": str(source.id), **event_payload},
        ))
        return True


async def check_ingesting_source(client, source) -> bool:
    """True means the *recorded* execution is RUNNING, never that media is ready.

    Return False on terminal, missing or unverifiable execution. The caller
    should skip redispatch, and may continue starting the worker for other work.
    """
    source_id = uuid.UUID(str(source.id))
    workflow_id = str(source.workflow_id)
    status = None
    try:
        description = await asyncio.wait_for(
            client.get_workflow_handle(workflow_id).describe(),
            timeout=TEMPORAL_DESCRIBE_TIMEOUT_SECONDS,
        )
        status = description.status
    except RPCError as exc:
        if exc.status == RPCStatusCode.NOT_FOUND:
            code = "ingest_temporal_execution_missing"
            state = "NOT_FOUND"
            retryable = False
        else:
            code = "ingest_temporal_visibility_unavailable"
            state = "UNVERIFIED"
            retryable = True
    except Exception:
        # Error text or workflow RPC errors may carry secrets. Only store
        # bounded, fixed diagnostic fields rather than raw exception messages.
        code = "ingest_temporal_visibility_unavailable"
        state = "UNVERIFIED"
        retryable = True
    else:
        if status == WorkflowExecutionStatus.RUNNING:
            _persist_ingest_diagnostic(source_id, workflow_id, None)
            return True
        code = "ingest_temporal_not_running"
        state = getattr(status, "name", "UNKNOWN")
        if not isinstance(state, str) or len(state) > 40:
            state = "UNKNOWN"
        retryable = False

    diagnostic: dict[str, object] = {
        "code": code,
        "workflow_id": workflow_id,
        "workflow_status": state,
        "retryable_visibility_check": retryable,
        "recovery": _RECOVERY_TEXT,
    }
    _persist_ingest_diagnostic(source_id, workflow_id, diagnostic)
    logger.warning(
        "ingest recovery held source_id=%s workflow_id=%s code=%s temporal_status=%s",
        source_id, workflow_id, code, state,
    )
    return False
