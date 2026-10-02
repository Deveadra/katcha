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
from katcha.models import SourceItem
from katcha.orchestration.activities import (
    enqueue_ingested_analysis_activity,
    ingest_source,
    mark_source_failed,
    prepare_authorized_passthrough_activity,
)
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
from katcha.orchestration.worker_group import run_worker_group
from katcha.orchestration.workflows import ClipIngestWorkflow
from katcha.publishing_models import Publication


async def _resume_persisted_ingest_and_publication_work(
    client: Client,
    settings,
) -> tuple[int, int]:
    with session_scope() as session:
        sources = list(
            session.scalars(
                select(SourceItem).where(
                    SourceItem.status.in_(["registered", "ingesting"]),
                    SourceItem.workflow_id.is_not(None),
                )
            )
        )
        publications = list(
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
    for row in sources:
        try:
            await client.start_workflow(
                ClipIngestWorkflow.run,
                str(row.id),
                id=str(row.workflow_id),
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
                task_queue=settings.temporal_task_queue,
            )
            resumed += 1
        except WorkflowAlreadyStartedError:
            present += 1

    for row in publications:
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
    resumed, present = await _resume_persisted_ingest_and_publication_work(
        client,
        settings,
    )
    logging.getLogger(__name__).info(
        "ingest/publication recovery reconciled work resumed=%s temporal_present=%s",
        resumed,
        present,
    )

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as activity_executor:
        ingest_worker = Worker(
            client,
            task_queue=settings.temporal_task_queue,
            workflows=[ClipIngestWorkflow],
            activities=[
                ingest_source,
                mark_source_failed,
                enqueue_ingested_analysis_activity,
                prepare_authorized_passthrough_activity,
            ],
            activity_executor=activity_executor,
        )
        publishing_worker = Worker(
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
        await run_worker_group([ingest_worker, publishing_worker])


if __name__ == "__main__":
    asyncio.run(main())
