from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

from katcha.orchestration.resource_queue import execute_resource_activity


@workflow.defn
class RankedShortEpisodeEditorialWorkflow:
    @workflow.run
    async def run(self, episode_id: str, start_stage: str = "script") -> dict[str, Any]:
        paid_once = RetryPolicy(maximum_attempts=1)
        local_retry = RetryPolicy(
            initial_interval=timedelta(seconds=5),
            backoff_coefficient=2.0,
            maximum_interval=timedelta(minutes=2),
            maximum_attempts=3,
        )
        voice_profile: object | None = None

        if start_stage in {"script", "voice"}:
            try:
                if start_stage == "script":
                    await execute_resource_activity(
                        "generate_episode_script_candidates",
                        episode_id,
                        start_to_close_timeout=timedelta(minutes=4),
                        retry_policy=paid_once,

                    )
                    await execute_resource_activity(
                        "select_episode_script_candidate",
                        episode_id,
                        start_to_close_timeout=timedelta(seconds=30),
                        retry_policy=local_retry,

                    )
                voiced = await execute_resource_activity(
                    "generate_episode_narration_assets",
                    episode_id,
                    start_to_close_timeout=timedelta(minutes=12),
                    retry_policy=paid_once,

                )
                voice_profile = voiced.get("voice_profile")
            except Exception as exc:
                await execute_resource_activity(
                    "mark_short_episode_failed",
                    args=[episode_id, str(exc)],
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )
                raise

            editorial = await execute_resource_activity(
                "advance_short_episode_editorial_automation_activity",
                episode_id,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=local_retry,

            )
            if editorial.get("action") != "editorial_approved":
                return {
                    "episode_id": episode_id,
                    "status": "voiced",
                    "voice_profile": voice_profile,
                    "automation": editorial,
                }
        elif start_stage != "render":
            raise ValueError(f"unsupported short episode editorial stage: {start_stage}")

        try:
            await execute_resource_activity(
                "build_ranked_episode_render_manifest_activity",
                episode_id,
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=local_retry,

            )
            rendered = await execute_resource_activity(
                "render_ranked_episode_activity",
                episode_id,
                start_to_close_timeout=timedelta(minutes=30),
                retry_policy=local_retry,

            )
        except Exception as exc:
            await execute_resource_activity(
                "mark_short_episode_failed",
                args=[episode_id, str(exc)],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            raise

        automation = await execute_resource_activity(
            "advance_short_episode_render_automation_activity",
            episode_id,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=local_retry,

        )
        publication_id = automation.get("publication_id")
        publication_workflow_id = automation.get("publication_workflow_id")
        handoff_started = False
        if publication_id and publication_workflow_id:
            try:
                await execute_resource_activity(
                    "start_registered_publication_activity",
                    args=[str(publication_id), str(publication_workflow_id)],
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=local_retry,

                )
                handoff_started = True
            except Exception:
                handoff_started = False

        return {
            "episode_id": episode_id,
            "status": automation.get("action") or "render_review",
            "voice_profile": voice_profile,
            "output_key": rendered.get("output_key"),
            "render_attempt_id": rendered.get("render_attempt_id"),
            "publication_id": publication_id,
            "publication_workflow_id": publication_workflow_id,
            "publication_handoff_started": handoff_started,
        }
