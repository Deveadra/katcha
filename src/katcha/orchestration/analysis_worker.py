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
from katcha.models import ClipAnalysisRun
from katcha.orchestration.analysis_activities import (
    build_local_intelligence,
    bulk_vision_analysis,
    deep_video_analysis,
    mark_analysis_failed,
    score_local_candidate,
)
from katcha.orchestration.analysis_workflows import ClipAnalysisWorkflow
from katcha.orchestration.similarity_activities import detect_near_duplicates


async def _resume_persisted_analysis_work(client: Client, settings) -> tuple[int, int]:
    with session_scope() as session:
        rows = list(
            session.scalars(
                select(ClipAnalysisRun).where(
                    ClipAnalysisRun.status.in_(["queued", "running"])
                )
            )
        )

    resumed = 0
    present = 0
    for row in rows:
        try:
            await client.start_workflow(
                ClipAnalysisWorkflow.run,
                args=[str(row.id), settings.ai_enabled],
                id=row.workflow_id,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                task_queue=settings.temporal_analysis_task_queue,
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
    resumed, present = await _resume_persisted_analysis_work(client, settings)
    logging.getLogger(__name__).info(
        "analysis recovery reconciled work resumed=%s temporal_present=%s",
        resumed,
        present,
    )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as activity_executor:
        worker = Worker(
            client,
            task_queue=settings.temporal_analysis_task_queue,
            workflows=[ClipAnalysisWorkflow],
            activities=[
                build_local_intelligence,
                detect_near_duplicates,
                bulk_vision_analysis,
                deep_video_analysis,
                score_local_candidate,
                mark_analysis_failed,
            ],
            activity_executor=activity_executor,
        )
        await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
