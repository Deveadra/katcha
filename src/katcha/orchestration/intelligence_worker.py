from __future__ import annotations

import asyncio
import concurrent.futures
import logging

from temporalio.client import Client
from temporalio.worker import Worker

from katcha.acquisition.runtime import DISCOVERY_TASK_QUEUE
from katcha.config import get_settings
from katcha.intelligence.runtime import INTELLIGENCE_TASK_QUEUE
from katcha.orchestration.discovery_activities import (
    execute_discovery_page_activity,
    finalize_topic_watch_execution_activity,
    mark_discovery_run_failed,
    prepare_topic_watch_execution_activity,
)
from katcha.orchestration.discovery_workflows import (
    DiscoveryRunWorkflow,
    TopicWatchScheduleWorkflow,
    TopicWatchWorkflow,
)
from katcha.orchestration.intelligence_activities import (
    apply_channel_safety_demotion_activity,
    compute_channel_economics_activity,
    compute_channel_schedule_activity,
    derive_channel_observations_activity,
    record_command_intelligence_workflow_lifecycle_activity,
    refresh_channel_growth_activity,
    refresh_edit_blueprint_performance_activity,
    refresh_packaging_intelligence_activity,
    refresh_trend_activation_performance_activity,
    run_channel_packaging_experiments_activity,
    run_channel_trend_activation_activity,
    run_clip_lifecycle_maintenance_activity,
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
from katcha.orchestration.trend_activities import (
    refresh_channel_trends_activity,
    refresh_trend_calibration_activity,
)
from katcha.orchestration.trend_workflows import (
    ChannelTrendCalibrationWorkflow,
    ChannelTrendRefreshWorkflow,
)
from katcha.orchestration.worker_group import run_worker_group
from katcha.trends.runtime import TREND_TASK_QUEUE


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

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as activity_executor:
        discovery_worker = Worker(
            client,
            task_queue=DISCOVERY_TASK_QUEUE,
            workflows=[
                DiscoveryRunWorkflow,
                TopicWatchWorkflow,
                TopicWatchScheduleWorkflow,
            ],
            activities=[
                execute_discovery_page_activity,
                mark_discovery_run_failed,
                prepare_topic_watch_execution_activity,
                finalize_topic_watch_execution_activity,
            ],
            activity_executor=activity_executor,
        )
        trend_worker = Worker(
            client,
            task_queue=TREND_TASK_QUEUE,
            workflows=[
                ChannelTrendRefreshWorkflow,
                ChannelTrendCalibrationWorkflow,
            ],
            activities=[
                refresh_channel_trends_activity,
                refresh_trend_calibration_activity,
            ],
            activity_executor=activity_executor,
        )
        intelligence_worker = Worker(
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
                record_command_intelligence_workflow_lifecycle_activity,
                refresh_channel_growth_activity,
                refresh_edit_blueprint_performance_activity,
                refresh_packaging_intelligence_activity,
                run_channel_packaging_experiments_activity,
                run_clip_lifecycle_maintenance_activity,
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
        await run_worker_group([discovery_worker, trend_worker, intelligence_worker])


if __name__ == "__main__":
    asyncio.run(main())
