from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from temporalio.api.enums.v1 import TaskQueueType
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest
from temporalio.client import WorkflowExecutionStatus

from katcha.acquisition.runtime import DISCOVERY_TASK_QUEUE
from katcha.config import get_settings
from katcha.intelligence.runtime import INTELLIGENCE_TASK_QUEUE
from katcha.orchestration.client import get_temporal_client
from katcha.trends.runtime import TREND_TASK_QUEUE

# Read-only Temporal diagnostics. Poller recency cannot certify a worker build,
# source ingestion, successful editorial activity, or rendered output.
_MAX_POLL_AGE = timedelta(minutes=3)
_MAX_CLOCK_SKEW = timedelta(seconds=10)
_PROBE_TIMEOUT_SECONDS = 5

_QUEUE_FIELDS = (
    ("intelligence", INTELLIGENCE_TASK_QUEUE),
    ("discovery", DISCOVERY_TASK_QUEUE),
    ("trend", TREND_TASK_QUEUE),
)


def _recent_poller_present(pollers: Any, *, now: datetime) -> bool:
    for poller in pollers:
        timestamp = getattr(poller, "last_access_time", None)
        if timestamp is None:
            continue
        try:
            last = timestamp.ToDatetime(tzinfo=UTC)
        except (AttributeError, TypeError, ValueError):
            continue
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        age = now - last.astimezone(UTC)
        if -_MAX_CLOCK_SKEW <= age <= _MAX_POLL_AGE:
            return True
    return False


async def inspect_trend_schedule_runtime(workflow_id: str) -> dict[str, Any]:
    """Inspect scheduled scan plus all real intake/scoring/editorial task queues."""
    checked_at = datetime.now(UTC)
    result: dict[str, Any] = {
        "checked_at": checked_at,
        "status": "unavailable",
        "schedule_running": False,
        "runtime_verified": False,
        "discovery_queue_ready": False,
        "trend_queue_ready": False,
        "editorial_queue_ready": False,
        "reason": "temporal_probe_unavailable",
    }
    queue_fields = (*_QUEUE_FIELDS, ("production", None))
    for prefix, _ in queue_fields:
        for kind in ("workflow", "activity"):
            result[f"{prefix}_{kind}_pollers_recent"] = False

    if not workflow_id:
        result["status"] = "not_registered"
        result["reason"] = "no_registered_workflow_id"
        return result

    try:
        client = await asyncio.wait_for(
            get_temporal_client(), timeout=_PROBE_TIMEOUT_SECONDS
        )
    except Exception:
        result["reason"] = "temporal_connection_unavailable"
        return result

    settings = get_settings()
    queues = (*_QUEUE_FIELDS, ("production", settings.temporal_production_task_queue))

    async def pollers_recent(queue: str, task_type: TaskQueueType) -> bool:
        response = await client.workflow_service.describe_task_queue(
            DescribeTaskQueueRequest(
                namespace=settings.temporal_namespace,
                task_queue=TaskQueue(name=queue),
                task_queue_type=task_type,
                report_pollers=True,
            ),
            timeout=timedelta(seconds=_PROBE_TIMEOUT_SECONDS),
        )
        return _recent_poller_present(response.pollers, now=checked_at)

    async def schedule_running() -> bool:
        description = await client.get_workflow_handle(workflow_id).describe()
        return description.status == WorkflowExecutionStatus.RUNNING

    queries = [schedule_running()]
    keys = ["schedule_running"]
    for prefix, queue in queues:
        for kind, task_type in (
            ("workflow", TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW),
            ("activity", TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY),
        ):
            keys.append(f"{prefix}_{kind}_pollers_recent")
            queries.append(pollers_recent(queue, task_type))

    try:
        outcomes = await asyncio.wait_for(
            asyncio.gather(*queries, return_exceptions=True),
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except Exception:
        result["status"] = "probe_timeout"
        result["reason"] = "temporal_probe_timed_out"
        return result

    failures = set()
    for key, outcome in zip(keys, outcomes, strict=True):
        if isinstance(outcome, BaseException):
            failures.add(key)
        else:
            result[key] = bool(outcome)

    if "schedule_running" in failures:
        result["status"] = "workflow_unavailable"
        result["reason"] = "schedule_describe_failed"
        return result
    if not result["schedule_running"]:
        result["status"] = "workflow_not_running"
        result["reason"] = "registered_schedule_not_running"
        return result

    for prefix, _queue in queues:
        work = f"{prefix}_workflow_pollers_recent"
        activity = f"{prefix}_activity_pollers_recent"
        verified = work not in failures and activity not in failures
        ready = verified and result[work] and result[activity]
        if prefix == "discovery":
            result["discovery_queue_ready"] = ready
        elif prefix == "trend":
            result["trend_queue_ready"] = ready
        elif prefix == "production":
            result["editorial_queue_ready"] = ready

    result["runtime_verified"] = bool(
        result["intelligence_workflow_pollers_recent"]
        and result["intelligence_activity_pollers_recent"]
        and result["discovery_queue_ready"]
        and result["trend_queue_ready"]
    )
    for prefix, _queue in queues:
        work = f"{prefix}_workflow_pollers_recent"
        activity = f"{prefix}_activity_pollers_recent"
        if work in failures or activity in failures:
            result["status"] = (
                "editorial_queue_unavailable" if prefix == "production"
                else f"{prefix}_queue_unavailable"
            )
            result["reason"] = f"{prefix}_poller_query_failed"
            return result
        if not result[work] or not result[activity]:
            result["status"] = (
                "editorial_worker_not_observed" if prefix == "production"
                else f"{prefix}_worker_not_observed"
            )
            result["reason"] = f"no_recent_{prefix}_workflow_and_activity_pollers"
            return result

    result["status"] = "queues_recently_polled"
    result["reason"] = None
    return result
