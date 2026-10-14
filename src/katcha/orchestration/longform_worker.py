from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from contextlib import suppress

from sqlalchemy import select
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.worker import Worker

from katcha.config import Settings, get_settings
from katcha.db import session_scope
from katcha.longform_models import Compilation
from katcha.orchestration.editorial_activities import EDITORIAL_ACTIVITIES
from katcha.orchestration.editorial_dispatch import editorial_reconciler
from katcha.orchestration.editorial_workflows import EditorialProjectWorkflow
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
from katcha.orchestration.scene_index_dispatch import (
    fail_scene_index_attempt,
    measure_project_media,
)
from katcha.orchestration.scene_index_workflows import SceneIndexWorkflow


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


def create_longform_worker(
    client: Client, settings: Settings, activity_executor: concurrent.futures.Executor,
) -> Worker:
    """Use the same Editorial and measured-media registrations in every deployment."""
    return Worker(
        client,
        task_queue=settings.temporal_longform_task_queue,
        workflows=[LongformCompilationWorkflow, EditorialProjectWorkflow, SceneIndexWorkflow],
        activities=[
            select_compilation_candidates_activity,
            generate_longform_editor_plan_activity,
            critique_longform_plan_activity,
            finalize_longform_plan_activity,
            generate_longform_narration_activity,
            build_longform_manifest_activity,
            render_longform_activity,
            mark_compilation_failed,
            *EDITORIAL_ACTIVITIES,
            measure_project_media,
            fail_scene_index_attempt,
        ],
        activity_executor=activity_executor,
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
    resumed, present = await _resume_persisted_longform_work(client, settings)
    logging.getLogger(__name__).info(
        "longform recovery reconciled work resumed=%s temporal_present=%s",
        resumed,
        present,
    )

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as activity_executor:
        worker = create_longform_worker(client, settings, activity_executor)
        reconciler = asyncio.create_task(editorial_reconciler(client))
        try:
            await worker.run()
        finally:
            reconciler.cancel()
            with suppress(asyncio.CancelledError):
                await reconciler


if __name__ == "__main__":
    asyncio.run(main())
