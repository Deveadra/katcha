"""Replay-safe, durable waiting for the single-host media resource budget."""

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.exceptions import ActivityError, ApplicationError

RESOURCE_BUSY = "LocalMediaResourceBusy"
PREFIX = "resource_queued_"
MEDIA_ACTIVITIES = frozenset({
    "ingest_source",
    "reconcile_remote_acquisition",
    "prepare_authorized_passthrough_activity",
    "detect_near_duplicates",
    "build_local_intelligence",
    "bulk_vision_analysis",
    "deep_video_analysis",
    "measure_project_media",
    "render_short_activity",
    "render_ranked_episode_activity",
    "render_longform_activity",
    "render_brand_preview_activity",
    "editorial_render",
    "editorial_generate_narration",
    "generate_narration_assets",
    "generate_longform_narration_activity",
    "generate_episode_narration_assets",
    "prepare_meme_assets",
    "upload_video_activity",
})


async def execute_resource_activity(name: str, *args: Any, **kwargs: Any) -> Any:
    if name not in MEDIA_ACTIVITIES or not workflow.patched("local-media-resource-queue-v1"):
        return await workflow.execute_activity(name, *args, **kwargs)
    while True:
        try:
            return await workflow.execute_activity(PREFIX + name, *args, **kwargs)
        except ActivityError as exc:
            cause = exc.cause
            if not isinstance(cause, ApplicationError) or cause.type != RESOURCE_BUSY:
                raise
            # A separate durable timer: wait time never consumes the operation's
            # execution timeout or normal provider failure retry budget.
            await workflow.sleep(timedelta(seconds=15))
