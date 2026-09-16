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
            elif start_stage == "voice":
                voiced = await workflow.execute_activity(
                    "generate_episode_narration_assets",
                    episode_id,
                    start_to_close_timeout=timedelta(minutes=12),
                    retry_policy=paid_once,
                    result_type=dict[str, object],
                )
            else:
                raise ValueError(f"unsupported short episode editorial stage: {start_stage}")
            return {
                "episode_id": episode_id,
                "status": "voiced",
                "voice_profile": voiced.get("voice_profile"),
            }
        except Exception as exc:
            await workflow.execute_activity(
                "mark_short_episode_failed",
                args=[episode_id, str(exc)],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            raise
