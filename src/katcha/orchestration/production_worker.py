from __future__ import annotations

import asyncio
import concurrent.futures
import logging

from temporalio.client import Client
from temporalio.worker import Worker

from katcha.config import get_settings
from katcha.orchestration.production_activities import (
    build_render_manifest_activity,
    generate_narration_assets,
    generate_script_candidates,
    mark_production_failed,
    render_short_activity,
    select_script_candidate,
)
from katcha.orchestration.production_workflows import ShortProductionWorkflow
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
            task_queue=settings.temporal_production_task_queue,
            workflows=[ShortProductionWorkflow, RankedShortEpisodeEditorialWorkflow],
            activities=[
                generate_script_candidates,
                select_script_candidate,
                generate_narration_assets,
                build_render_manifest_activity,
                render_short_activity,
                mark_production_failed,
                generate_episode_script_candidates,
                select_episode_script_candidate,
                generate_episode_narration_assets,
                build_ranked_episode_render_manifest_activity,
                render_ranked_episode_activity,
                mark_short_episode_failed,
            ],
            activity_executor=activity_executor,
        )
        await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
