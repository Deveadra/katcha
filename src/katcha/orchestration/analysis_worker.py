from __future__ import annotations

import asyncio
import concurrent.futures
import logging

from temporalio.client import Client
from temporalio.worker import Worker

from katcha.config import get_settings
from katcha.orchestration.analysis_activities import (
    build_local_intelligence,
    mark_analysis_failed,
    score_local_candidate,
)
from katcha.orchestration.analysis_workflows import ClipAnalysisWorkflow


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

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as activity_executor:
        worker = Worker(
            client,
            task_queue=settings.temporal_analysis_task_queue,
            workflows=[ClipAnalysisWorkflow],
            activities=[build_local_intelligence, score_local_candidate, mark_analysis_failed],
            activity_executor=activity_executor,
        )
        await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
