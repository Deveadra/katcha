from __future__ import annotations

import asyncio
import concurrent.futures
import logging

from sqlalchemy import select
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.worker import Worker

from katcha.config import get_settings
from katcha.db import session_scope
from katcha.orchestration.packaging_activities import (
    apply_packaging_text_activity,
    apply_packaging_thumbnail_activity,
    finalize_packaging_activation_activity,
    mark_packaging_activation_failed,
    prepare_packaging_activation_activity,
)
from katcha.orchestration.packaging_workflows import YouTubePackagingActivationWorkflow
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
from katcha.orchestration.reach_activities import (
    create_reach_reporting_job_activity,
    prepare_reach_reporting_job_activity,
    sync_reach_reports_activity,
)
from katcha.orchestration.reach_workflows import YouTubeReachSyncWorkflow
from katcha.publishing_models import Publication


async def _resume_persisted_publications(client: Client, settings) -> tuple[int, int]:
    with session_scope() as session:
        rows = list(
            session.scalars(
                select(Publication).where(
                    Publication.status.in_(
                        ["queued", "uploading", "uploaded", "processing"]
                    )
                )
            )
        )

    resumed = 0
    present = 0
    for row in rows:
        try:
            await client.start_workflow(
                YouTubePublicationWorkflow.run,
                args=[
                    str(row.id),
                    settings.youtube_processing_poll_seconds,
                    settings.youtube_processing_max_polls,
                    settings.analytics_offsets_hours(),
                ],
                id=row.workflow_id,
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
                task_queue=settings.temporal_publishing_task_queue,
            )
            resumed += 1
        except WorkflowAlreadyStartedError:
            present += 1
    return resumed, present


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
    resumed, present = await _resume_persisted_publications(client, settings)
    logging.getLogger(__name__).info(
        "publication recovery reconciled work resumed=%s temporal_present=%s",
        resumed,
        present,
    )

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as activity_executor:
        worker = Worker(
            client,
            task_queue=settings.temporal_publishing_task_queue,
            workflows=[
                YouTubePublicationWorkflow,
                YouTubePackagingActivationWorkflow,
                YouTubeReachSyncWorkflow,
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
                prepare_packaging_activation_activity,
                apply_packaging_text_activity,
                apply_packaging_thumbnail_activity,
                finalize_packaging_activation_activity,
                mark_packaging_activation_failed,
                prepare_reach_reporting_job_activity,
                create_reach_reporting_job_activity,
                sync_reach_reports_activity,
            ],
            activity_executor=activity_executor,
        )
        await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
