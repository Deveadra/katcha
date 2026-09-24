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
    refresh_edit_blueprint_performance_activity,
    refresh_packaging_intelligence_activity,
    refresh_trend_activation_performance_activity,
    run_channel_packaging_experiments_activity,
    run_channel_trend_activation_activity,
    schedule_channel_reach_sync_activity,
    seed_channel_packaging_activity,
    train_channel_ranking_activity,
)
from katcha.orchestration.intelligence_workflows import (
    ChannelIntelligenceRefreshWorkflow,
    ChannelIntelligenceScheduleWorkflow,
    ChannelTrendActivationPerformanceWorkflow,
    ChannelTrendActivationScheduleWorkflow,
    ChannelTrendActivationWorkflow,
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
                ChannelTrendActivationWorkflow,
                ChannelTrendActivationPerformanceWorkflow,
                ChannelTrendActivationScheduleWorkflow,
            ],
            activities=[
                derive_channel_observations_activity,
                refresh_edit_blueprint_performance_activity,
                refresh_packaging_intelligence_activity,
                run_channel_packaging_experiments_activity,
                seed_channel_packaging_activity,
                train_channel_ranking_activity,
                compute_channel_economics_activity,
                compute_channel_schedule_activity,
                apply_channel_safety_demotion_activity,
                run_channel_trend_activation_activity,
                refresh_trend_activation_performance_activity,
                schedule_channel_reach_sync_activity,
            ],
            activity_executor=activity_executor,
        )
        await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
