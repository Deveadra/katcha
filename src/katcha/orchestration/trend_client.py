from __future__ import annotations

from temporalio.exceptions import WorkflowAlreadyStartedError

from katcha.acquisition.runtime import DISCOVERY_TASK_QUEUE
from katcha.orchestration.client import get_temporal_client
from katcha.orchestration.discovery_workflows import (
    TopicWatchScheduleWorkflow,
    TopicWatchWorkflow,
)


async def start_topic_watch_workflow(
    topic_watch_id: str,
    workflow_id: str,
    *,
    execution_key: str,
    top_n: int,
) -> str:
    client = await get_temporal_client()
    try:
        handle = await client.start_workflow(
            TopicWatchWorkflow.run,
            args=[topic_watch_id, execution_key, top_n],
            id=workflow_id,
            task_queue=DISCOVERY_TASK_QUEUE,
        )
    except WorkflowAlreadyStartedError:
        handle = client.get_workflow_handle(workflow_id)
    return handle.id


async def start_topic_watch_schedule(
    topic_watch_id: str,
    workflow_id: str,
    *,
    interval_minutes: int,
    top_n: int,
) -> str:
    client = await get_temporal_client()
    try:
        handle = await client.start_workflow(
            TopicWatchScheduleWorkflow.run,
            args=[topic_watch_id, interval_minutes, top_n, 0],
            id=workflow_id,
            task_queue=DISCOVERY_TASK_QUEUE,
        )
    except WorkflowAlreadyStartedError:
        handle = client.get_workflow_handle(workflow_id)
    return handle.id
