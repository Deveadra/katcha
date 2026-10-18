from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from temporalio.api.enums.v1 import TaskQueueType
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest
from temporalio.client import WorkflowExecutionStatus

from katcha.config import get_settings
from katcha.intelligence.runtime import INTELLIGENCE_TASK_QUEUE
from katcha.orchestration.client import get_temporal_client

# Read-only Temporal diagnostics. A recent poll is evidence that some worker
# polled the queue; it does not certify the code/version or downstream render.
_MAX_POLL_AGE = timedelta(minutes=3)
_MAX_CLOCK_SKEW = timedelta(seconds=10)
_PROBE_TIMEOUT_SECONDS = 5


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
    """Probe schedule execution and both task queues without starting work."""
    checked_at = datetime.now(UTC)
    result: dict[str, Any] = {
        "checked_at": checked_at,
        "status": "unavailable",
        "schedule_running": False,
        "intelligence_workflow_pollers_recent": False,
        "intelligence_activity_pollers_recent": False,
        "production_workflow_pollers_recent": False,
        "production_activity_pollers_recent": False,
        "runtime_verified": False,
        "editorial_queue_ready": False,
        "reason": "temporal_probe_unavailable",
    }
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

    queries = [
        schedule_running(),
        pollers_recent(
            INTELLIGENCE_TASK_QUEUE,
            TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW,
        ),
        pollers_recent(
            INTELLIGENCE_TASK_QUEUE,
            TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY,
        ),
        pollers_recent(
            settings.temporal_production_task_queue,
            TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW,
        ),
        pollers_recent(
            settings.temporal_production_task_queue,
            TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY,
        ),
    ]
    try:
        outcomes = await asyncio.wait_for(
            asyncio.gather(*queries, return_exceptions=True),
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except Exception:
        result["status"] = "probe_timeout"
        result["reason"] = "temporal_probe_timed_out"
        return result

    keys = [
        "schedule_running",
        "intelligence_workflow_pollers_recent",
        "intelligence_activity_pollers_recent",
        "production_workflow_pollers_recent",
        "production_activity_pollers_recent",
    ]
    for key, outcome in zip(keys, outcomes, strict=True):
        if not isinstance(outcome, BaseException):
            result[key] = bool(outcome)

    if isinstance(outcomes[0], BaseException):
        result["status"] = "workflow_unavailable"
        result["reason"] = "schedule_describe_failed"
        return result
    if not result["schedule_running"]:
        result["status"] = "workflow_not_running"
        result["reason"] = "registered_schedule_not_running"
        return result
    if any(isinstance(x, BaseException) for x in outcomes[1:3]):
        result["status"] = "intelligence_queue_unavailable"
        result["reason"] = "intelligence_poller_query_failed"
        return result
    if not (
        result["intelligence_workflow_pollers_recent"]
        and result["intelligence_activity_pollers_recent"]
    ):
        result["status"] = "intelligence_worker_not_observed"
        result["reason"] = "no_recent_intelligence_workflow_and_activity_pollers"
        return result

    result["runtime_verified"] = True
    if any(isinstance(x, BaseException) for x in outcomes[3:]):
        result["status"] = "editorial_queue_unavailable"
        result["reason"] = "production_poller_query_failed"
    elif not (
        result["production_workflow_pollers_recent"]
        and result["production_activity_pollers_recent"]
    ):
        result["status"] = "editorial_worker_not_observed"
        result["reason"] = "no_recent_production_workflow_and_activity_pollers"
    else:
        result["status"] = "queues_recently_polled"
        result["editorial_queue_ready"] = True
        result["reason"] = None
    return result
