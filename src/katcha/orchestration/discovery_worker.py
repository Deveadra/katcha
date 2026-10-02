from __future__ import annotations

import asyncio
import concurrent.futures
import logging

from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.worker import Worker

from katcha.acquisition.runtime import DISCOVERY_TASK_QUEUE
from katcha.config import get_settings
from katcha.orchestration.discovery_activities import (
    execute_discovery_page_activity,
    finalize_topic_watch_execution_activity,
    mark_discovery_run_failed,
    prepare_command_discovery_candidates_activity,
    prepare_topic_watch_execution_activity,
    record_command_source_prepare_lifecycle_activity,
    record_topic_watch_command_cycle_activity,
)
from katcha.orchestration.discovery_workflows import (
    CommandSourcePrepareWorkflow,
    DiscoveryRunWorkflow,
    TopicWatchScheduleWorkflow,
    TopicWatchWorkflow,
)
from katcha.services.ingestion_sources import list_resumable_source_runs


async def _resume_incomplete_source_runs(client: Client) -> tuple[int, int]:
    resumed = 0
    already_present = 0
    for run in list_resumable_source_runs():
        workflow_id = f"discovery-run-{run.id}"
        try:
            await client.start_workflow(
                DiscoveryRunWorkflow.run,
                str(run.id),
                id=workflow_id,
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
                task_queue=DISCOVERY_TASK_QUEUE,
            )
            resumed += 1
        except WorkflowAlreadyStartedError:
            # Existing Temporal history means the durable workflow already owns
            # recovery. REJECT_DUPLICATE prevents replaying a closed execution.
            already_present += 1
    return resumed, already_present


async def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    client = await Client.connect(
        settings.temporal_host,
        namespace=settings.temporal_namespace,
    )
    resumed, already_present = await _resume_incomplete_source_runs(client)
    logging.getLogger(__name__).info(
        "discovery recovery reconciled source runs resumed=%s temporal_present=%s",
        resumed,
        already_present,
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as activity_executor:
        worker = Worker(
            client,
            task_queue=DISCOVERY_TASK_QUEUE,
            workflows=[
                CommandSourcePrepareWorkflow,
                DiscoveryRunWorkflow,
                TopicWatchWorkflow,
                TopicWatchScheduleWorkflow,
            ],
            activities=[
                execute_discovery_page_activity,
                prepare_command_discovery_candidates_activity,
                record_command_source_prepare_lifecycle_activity,
                mark_discovery_run_failed,
                prepare_topic_watch_execution_activity,
                finalize_topic_watch_execution_activity,
                record_topic_watch_command_cycle_activity,
            ],
            activity_executor=activity_executor,
        )
        await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
