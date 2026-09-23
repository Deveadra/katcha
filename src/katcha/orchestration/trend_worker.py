from __future__ import annotations

import asyncio
import concurrent.futures
import logging

from temporalio.client import Client
from temporalio.worker import Worker

from katcha.config import get_settings
from katcha.orchestration.trend_activities import (
    refresh_channel_trends_activity,
    refresh_trend_calibration_activity,
)
from katcha.orchestration.trend_workflows import (
    ChannelTrendCalibrationWorkflow,
    ChannelTrendRefreshWorkflow,
)
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
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as activity_executor:
        worker = Worker(
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
        await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
