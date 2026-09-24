from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy


@workflow.defn
class RankedShortEpisodeEditorialWorkflow:
    @workflow.run
    async def run(self, episode_id: str, start_stage: str = "script") -> dict[str, object]:
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
                    await workflow.execute_activity(
                        "generate_episode_script_candidates",
                        episode_id,
                        start_to_close_timeout=timedelta(minutes=4),
                        retry_policy=paid_once,
                        result_type=dict[str, object],
                    )
                    await workflow.execute_activity(
                        "select_episode_script_candidate",
                        episode_id,
                        start_to_close_timeout=timedelta(seconds=30),
                        retry_policy=local_retry,
                        result_type=dict[str, object],
                    )
                voiced = await workflow.execute_activity(
                    "generate_episode_narration_assets",
                    episode_id,
                    start_to_close_timeout=timedelta(minutes=12),
                    retry_policy=paid_once,
                    result_type=dict[str, object],
                )
                voice_profile = voiced.get("voice_profile")
            except Exception as exc:
                await workflow.execute_activity(
                    "mark_short_episode_failed",
                    args=[episode_id, str(exc)],
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )
                raise

            editorial = await workflow.execute_activity(
                "advance_short_episode_editorial_automation_activity",
                episode_id,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=local_retry,
                result_type=dict[str, object],
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
            await workflow.execute_activity(
                "build_ranked_episode_render_manifest_activity",
                episode_id,
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=local_retry,
                result_type=dict[str, object],
            )
            rendered = await workflow.execute_activity(
                "render_ranked_episode_activity",
                episode_id,
                start_to_close_timeout=timedelta(minutes=30),
                retry_policy=local_retry,
                result_type=dict[str, object],
            )
        except Exception as exc:
            await workflow.execute_activity(
                "mark_short_episode_failed",
                args=[episode_id, str(exc)],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            raise

        automation = await workflow.execute_activity(
            "advance_short_episode_render_automation_activity",
            episode_id,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=local_retry,
            result_type=dict[str, object],
        )
        publication_id = automation.get("publication_id")
        publication_workflow_id = automation.get("publication_workflow_id")
        handoff_started = False
        if publication_id and publication_workflow_id:
            try:
                await workflow.execute_activity(
                    "start_registered_publication_activity",
                    args=[str(publication_id), str(publication_workflow_id)],
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=local_retry,
                    result_type=dict[str, object],
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
