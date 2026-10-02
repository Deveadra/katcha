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
from katcha.longform_models import Compilation
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


def _recovery_stage(row: Compilation) -> str | None:
    status = str(row.status or "").lower()
    if status in {"queued", "selecting"}:
        return "select"
    if status in {"planning", "critiquing"}:
        return "plan"
    if status in {"scripted", "voicing"}:
        return "voice"
    if status in {"voiced", "rendering"}:
        return "render"
    return None


async def _resume_persisted_longform_work(client: Client, settings) -> tuple[int, int]:
    with session_scope() as session:
        rows = list(
            session.scalars(
                select(Compilation).where(
                    Compilation.status.in_(
                        [
                            "queued",
                            "selecting",
                            "planning",
                            "critiquing",
                            "scripted",
                            "voicing",
                            "voiced",
                            "rendering",
                        ]
                    )
                )
            )
        )

    resumed = 0
    present = 0
    for row in rows:
        stage = _recovery_stage(row)
        if stage is None:
            continue
        try:
            await client.start_workflow(
                LongformCompilationWorkflow.run,
                args=[str(row.id), stage],
                id=row.workflow_id,
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
                task_queue=settings.temporal_longform_task_queue,
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
    resumed, present = await _resume_persisted_longform_work(client, settings)
    logging.getLogger(__name__).info(
        "longform recovery reconciled work resumed=%s temporal_present=%s",
        resumed,
        present,
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
