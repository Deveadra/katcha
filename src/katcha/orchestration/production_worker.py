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
from katcha.orchestration.brand_preview_activities import (
    mark_brand_preview_failed_activity,
    render_brand_preview_activity,
)
from katcha.orchestration.brand_preview_workflows import StagedBrandPreviewWorkflow
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
from katcha.orchestration.production_activities import (
    build_render_manifest_activity,
    generate_narration_assets,
    generate_script_candidates,
    mark_production_failed,
    render_short_activity,
    select_script_candidate,
)
from katcha.orchestration.production_workflows import ShortProductionWorkflow
from katcha.orchestration.render_automation_activities import (
    advance_production_render_automation_activity,
    advance_short_episode_editorial_automation_activity,
    advance_short_episode_render_automation_activity,
    start_registered_publication_activity,
)
from katcha.orchestration.short_episode_activities import (
    generate_episode_narration_assets,
    generate_episode_script_candidates,
    mark_short_episode_failed,
    select_episode_script_candidate,
)
from katcha.orchestration.short_episode_render_activities import (
    build_ranked_episode_render_manifest_activity,
    render_ranked_episode_activity,
)
from katcha.orchestration.short_episode_workflows import RankedShortEpisodeEditorialWorkflow
from katcha.orchestration.worker_group import run_worker_group
from katcha.production_models import Production
from katcha.services.brand_assets import seed_builtin_brand_assets
from katcha.short_episode_models import ShortEpisode


def _production_recovery_stage(row: Production) -> str | None:
    status = str(row.status or "").lower()
    if status in {"queued", "scripting"}:
        return "script"
    if status in {"scripted", "voicing"}:
        return "voice"
    if status in {"voiced", "rendering"}:
        return "render"
    return None


def _episode_recovery_stage(row: ShortEpisode) -> str | None:
    status = str(row.status or "").lower()
    stage = str(row.stage or "").lower()
    if status == "planned" and stage in {"regenerate_script_queued", "script_queued"}:
        return "script"
    if status == "scripting":
        return "script"
    if status in {"scripted", "voicing"}:
        return "voice"
    if status == "planned" and stage == "regenerate_voice_queued":
        return "voice"
    if status in {"editorial_approved", "rendering"}:
        return "render"
    if status == "planned" and stage == "render_regeneration_queued":
        return "render"
    return None


def _compilation_recovery_stage(row: Compilation) -> str | None:
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


async def _resume_persisted_production_work(client: Client, settings) -> tuple[int, int]:
    with session_scope() as session:
        productions = list(
            session.scalars(
                select(Production).where(
                    Production.status.in_(
                        ["queued", "scripting", "scripted", "voicing", "voiced", "rendering"]
                    )
                )
            )
        )
        episodes = list(
            session.scalars(
                select(ShortEpisode).where(
                    ShortEpisode.status.in_(
                        [
                            "planned",
                            "scripting",
                            "scripted",
                            "voicing",
                            "editorial_approved",
                            "rendering",
                        ]
                    )
                )
            )
        )
        compilations = list(
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
    work: list[tuple[object, str, str, str, str]] = []
    for row in productions:
        stage = _production_recovery_stage(row)
        if stage:
            work.append(
                (
                    ShortProductionWorkflow.run,
                    str(row.id),
                    row.workflow_id,
                    settings.temporal_production_task_queue,
                    stage,
                )
            )
    for row in episodes:
        stage = _episode_recovery_stage(row)
        if stage:
            workflow_id = f"{row.workflow_id}-editorial-{stage}"
            work.append(
                (
                    RankedShortEpisodeEditorialWorkflow.run,
                    str(row.id),
                    workflow_id,
                    settings.temporal_production_task_queue,
                    stage,
                )
            )
    for row in compilations:
        stage = _compilation_recovery_stage(row)
        if stage:
            work.append(
                (
                    LongformCompilationWorkflow.run,
                    str(row.id),
                    row.workflow_id,
                    settings.temporal_longform_task_queue,
                    stage,
                )
            )

    for workflow_run, source_id, workflow_id, task_queue, stage in work:
        try:
            await client.start_workflow(
                workflow_run,
                args=[source_id, stage],
                id=workflow_id,
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
                task_queue=task_queue,
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
    seeded_assets = seed_builtin_brand_assets()
    logging.getLogger(__name__).info(
        "verified %s built-in brand assets", len(seeded_assets)
    )
    client = await Client.connect(
        settings.temporal_host,
        namespace=settings.temporal_namespace,
    )
    resumed, present = await _resume_persisted_production_work(client, settings)
    logging.getLogger(__name__).info(
        "production recovery reconciled work resumed=%s temporal_present=%s",
        resumed,
        present,
    )

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as activity_executor:
        production_worker = Worker(
            client,
            task_queue=settings.temporal_production_task_queue,
            workflows=[
                ShortProductionWorkflow,
                RankedShortEpisodeEditorialWorkflow,
                StagedBrandPreviewWorkflow,
            ],
            activities=[
                render_brand_preview_activity,
                mark_brand_preview_failed_activity,
                generate_script_candidates,
                select_script_candidate,
                generate_narration_assets,
                build_render_manifest_activity,
                render_short_activity,
                mark_production_failed,
                advance_production_render_automation_activity,
                advance_short_episode_editorial_automation_activity,
                advance_short_episode_render_automation_activity,
                start_registered_publication_activity,
                generate_episode_script_candidates,
                select_episode_script_candidate,
                generate_episode_narration_assets,
                build_ranked_episode_render_manifest_activity,
                render_ranked_episode_activity,
                mark_short_episode_failed,
            ],
            activity_executor=activity_executor,
        )
        longform_worker = Worker(
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
        await run_worker_group([production_worker, longform_worker])


if __name__ == "__main__":
    asyncio.run(main())
