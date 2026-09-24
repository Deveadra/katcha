from __future__ import annotations

import uuid

from temporalio import activity

from katcha.services.render_automation import (
    advance_render_automation,
    advance_short_episode_editorial_automation,
)


@activity.defn
def advance_production_render_automation_activity(
    production_id: str,
) -> dict[str, object]:
    return advance_render_automation(
        "production",
        uuid.UUID(production_id),
    ).payload()


@activity.defn
def advance_short_episode_editorial_automation_activity(
    episode_id: str,
) -> dict[str, object]:
    return advance_short_episode_editorial_automation(
        uuid.UUID(episode_id)
    ).payload()


@activity.defn
def advance_short_episode_render_automation_activity(
    episode_id: str,
) -> dict[str, object]:
    return advance_render_automation(
        "short_episode",
        uuid.UUID(episode_id),
    ).payload()


@activity.defn
async def start_registered_publication_activity(
    publication_id: str,
    workflow_id: str,
) -> dict[str, object]:
    from katcha.orchestration.client import start_publication_workflow

    started_workflow_id = await start_publication_workflow(
        publication_id,
        workflow_id,
    )
    return {
        "publication_id": publication_id,
        "workflow_id": started_workflow_id,
        "started": True,
    }
