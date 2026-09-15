from __future__ import annotations

import asyncio
import concurrent.futures
import logging

from temporalio.client import Client
from temporalio.worker import Worker

from katcha.config import get_settings
from katcha.intelligence.runtime import INTELLIGENCE_TASK_QUEUE
from katcha.orchestration.intelligence_activities import (
    apply_channel_safety_demotion_activity,
    compute_channel_economics_activity,
    compute_channel_schedule_activity,
    derive_channel_observations_activity,
    train_channel_ranking_activity,
)
from katcha.orchestration.intelligence_workflows import (
    ChannelIntelligenceRefreshWorkflow,
    ChannelIntelligenceScheduleWorkflow,
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
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as activity_executor:
        worker = Worker(
            client,
            task_queue=INTELLIGENCE_TASK_QUEUE,
            workflows=[
                ChannelIntelligenceRefreshWorkflow,
                ChannelIntelligenceScheduleWorkflow,
            ],
            activities=[
                derive_channel_observations_activity,
                train_channel_ranking_activity,
                compute_channel_economics_activity,
                compute_channel_schedule_activity,
                apply_channel_safety_demotion_activity,
            ],
            activity_executor=activity_executor,
        )
        await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
