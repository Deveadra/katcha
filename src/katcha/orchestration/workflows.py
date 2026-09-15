from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy


@workflow.defn
class ClipIngestWorkflow:
    @workflow.run
    async def run(self, source_id: str) -> dict[str, str | bool]:
        retry_policy = RetryPolicy(
            initial_interval=timedelta(seconds=10),
            backoff_coefficient=2.0,
            maximum_interval=timedelta(minutes=5),
            maximum_attempts=4,
        )
        try:
            return await workflow.execute_activity(
                "ingest_source",
                source_id,
                start_to_close_timeout=timedelta(minutes=20),
                retry_policy=retry_policy,
                result_type=dict[str, str | bool],
            )
        except Exception as exc:
            await workflow.execute_activity(
                "mark_source_failed",
                args=[source_id, str(exc)],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            raise
