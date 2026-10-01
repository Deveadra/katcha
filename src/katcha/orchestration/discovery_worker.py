from __future__ import annotations

import asyncio
import concurrent.futures
import logging

from temporalio.client import Client
from temporalio.worker import Worker

from katcha.acquisition.runtime import DISCOVERY_TASK_QUEUE
from katcha.config import get_settings
from katcha.orchestration.discovery_activities import (
    execute_discovery_page_activity,
    prepare_command_discovery_candidates_activity,
    record_command_source_prepare_lifecycle_activity,
    finalize_topic_watch_execution_activity,
    mark_discovery_run_failed,
    prepare_topic_watch_execution_activity,
    record_topic_watch_command_cycle_activity,
)
from katcha.orchestration.discovery_workflows import (
    CommandSourcePrepareWorkflow,
    DiscoveryRunWorkflow,
    TopicWatchScheduleWorkflow,
    TopicWatchWorkflow,
)


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
