from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy


@workflow.defn
class ClipAnalysisWorkflow:
    @workflow.run
    async def run(self, run_id: str, ai_enabled: bool) -> dict[str, object]:
        media_retry = RetryPolicy(
            initial_interval=timedelta(seconds=15),
            backoff_coefficient=2.0,
            maximum_interval=timedelta(minutes=3),
            maximum_attempts=3,
        )
        try:
            await workflow.execute_activity(
                "build_local_intelligence",
                run_id,
                start_to_close_timeout=timedelta(minutes=30),
                retry_policy=media_retry,

            )
            await workflow.execute_activity(
                "detect_near_duplicates",
                run_id,
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(maximum_attempts=3),

            )
            if ai_enabled:
                bulk = await workflow.execute_activity(
                    "bulk_vision_analysis",
                    run_id,
                    start_to_close_timeout=timedelta(minutes=5),
                    retry_policy=RetryPolicy(maximum_attempts=3),

                )
                if bool(bulk.get("requires_deep_video")) and bool(bulk.get("deep_available")):
                    await workflow.execute_activity(
                        "deep_video_analysis",
                        run_id,
                        start_to_close_timeout=timedelta(minutes=12),
                        retry_policy=RetryPolicy(maximum_attempts=2),

                    )
            return await workflow.execute_activity(
                "score_local_candidate",
                run_id,
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(maximum_attempts=3),

            )
        except Exception as exc:
            await workflow.execute_activity(
                "mark_analysis_failed",
                args=[run_id, str(exc)],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            raise
