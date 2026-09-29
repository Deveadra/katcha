from __future__ import annotations

import asyncio
import concurrent.futures
import logging

from temporalio.client import Client
from temporalio.worker import Worker

from katcha.config import get_settings
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
from katcha.services.brand_assets import seed_builtin_brand_assets


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
