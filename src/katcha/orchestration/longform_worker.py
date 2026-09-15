from __future__ import annotations

import asyncio
import concurrent.futures
import logging

from temporalio.client import Client
from temporalio.worker import Worker

from katcha.config import get_settings
from katcha.orchestration.longform_activities import (
    build_longform_manifest_activity,
    critique_longform_plan_activity,
    finalize_longform_plan_activity,
    generate_longform_editor_plan_activity,
    generate_longform_narration_activity,
    mark_compilation_failed,
    render_longform_activity,
    select_compilation_candidates_activity,
)
from katcha.orchestration.longform_workflows import LongformCompilationWorkflow


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
            task_queue=settings.temporal_longform_task_queue,
            workflows=[LongformCompilationWorkflow],
            activities=[
                select_compilation_candidates_activity,
                generate_longform_editor_plan_activity,
                critique_longform_plan_activity,
                finalize_longform_plan_activity,
                generate_longform_narration_activity,
                build_longform_manifest_activity,
                render_longform_activity,
                mark_compilation_failed,
            ],
            activity_executor=activity_executor,
        )
        await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
