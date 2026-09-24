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
        render_attempt_id: str | None = None
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
                return {
                    "episode_id": episode_id,
                    "status": "voiced",
                    "voice_profile": voiced.get("voice_profile"),
                }
            if start_stage == "voice":
                voiced = await workflow.execute_activity(
                    "generate_episode_narration_assets",
                    episode_id,
                    start_to_close_timeout=timedelta(minutes=12),
                    retry_policy=paid_once,
                    result_type=dict[str, object],
                )
                return {
                    "episode_id": episode_id,
                    "status": "voiced",
                    "voice_profile": voiced.get("voice_profile"),
                }
            if start_stage == "render":
                attempt = await workflow.execute_activity(
                    "begin_render_attempt_activity",
                    args=[
                        "short_episode",
                        episode_id,
                        workflow.info().workflow_id,
                    ],
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=local_retry,
                    result_type=dict[str, object],
                )
                render_attempt_id = str(attempt["render_attempt_id"])
                render_generation = int(attempt["attempt_number"])
                await workflow.execute_activity(
                    "build_ranked_episode_render_manifest_activity",
                    args=[episode_id, render_generation],
                    start_to_close_timeout=timedelta(minutes=2),
                    retry_policy=local_retry,
                    result_type=dict[str, object],
                )
                await workflow.execute_activity(
                    "pre_render_qc_activity",
                    args=["short_episode", episode_id, render_attempt_id],
                    start_to_close_timeout=timedelta(minutes=2),
                    retry_policy=local_retry,
                    result_type=dict[str, object],
                )
                rendered = await workflow.execute_activity(
                    "render_ranked_episode_activity",
                    args=[episode_id, render_generation],
                    start_to_close_timeout=timedelta(minutes=30),
                    retry_policy=local_retry,
                    result_type=dict[str, object],
                )
                await workflow.execute_activity(
                    "post_render_qc_activity",
                    args=["short_episode", episode_id, render_attempt_id],
                    start_to_close_timeout=timedelta(minutes=2),
                    retry_policy=local_retry,
                    result_type=dict[str, object],
                )
                return {
                    "episode_id": episode_id,
                    "status": "rendered",
                    "output_key": rendered.get("output_key"),
                    "render_attempt_id": render_attempt_id,
                    "render_attempt_number": render_generation,
                }
            raise ValueError(f"unsupported short episode editorial stage: {start_stage}")
        except Exception as exc:
            if render_attempt_id is not None:
                await workflow.execute_activity(
                    "dead_letter_render_attempt_activity",
                    args=[render_attempt_id, str(exc)],
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )
            await workflow.execute_activity(
                "mark_short_episode_failed",
                args=[episode_id, str(exc)],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            raise
