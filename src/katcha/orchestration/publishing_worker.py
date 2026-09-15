from __future__ import annotations

import asyncio
import concurrent.futures
import logging

from temporalio.client import Client
from temporalio.worker import Worker

from katcha.config import get_settings
from katcha.orchestration.publishing_activities import (
    collect_analytics_snapshot_activity,
    finalize_publication_activity,
    initiate_upload_session_activity,
    mark_analytics_observation_failed,
    mark_processing_timeout_activity,
    mark_publication_failed,
    prepare_publication_activity,
    refresh_video_status_activity,
    upload_video_activity,
)
from katcha.orchestration.publishing_workflows import (
    YouTubeAnalyticsRefreshWorkflow,
    YouTubeAnalyticsWorkflow,
    YouTubePublicationWorkflow,
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
            task_queue=settings.temporal_publishing_task_queue,
            workflows=[
                YouTubePublicationWorkflow,
                YouTubeAnalyticsWorkflow,
                YouTubeAnalyticsRefreshWorkflow,
            ],
            activities=[
                prepare_publication_activity,
                initiate_upload_session_activity,
                upload_video_activity,
                refresh_video_status_activity,
                mark_processing_timeout_activity,
                finalize_publication_activity,
                collect_analytics_snapshot_activity,
                mark_analytics_observation_failed,
                mark_publication_failed,
            ],
            activity_executor=activity_executor,
        )
        await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
