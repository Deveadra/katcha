from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy


@workflow.defn
class StagedBrandPreviewWorkflow:
    @workflow.run
    async def run(self, preview_id: str) -> dict[str, object]:
        retry = RetryPolicy(
            initial_interval=timedelta(seconds=5),
            backoff_coefficient=2.0,
            maximum_interval=timedelta(minutes=2),
            maximum_attempts=3,
        )
        try:
            return await workflow.execute_activity(
                "render_brand_preview_activity",
                preview_id,
                start_to_close_timeout=timedelta(minutes=30),
                retry_policy=retry,

            )
        except Exception as exc:
            await workflow.execute_activity(
                "mark_brand_preview_failed_activity",
                args=[preview_id, str(exc)],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            raise
