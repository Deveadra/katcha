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
            result = await workflow.execute_activity(
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
        # Preserve replay compatibility for ingests already in Temporal history.
        # Analysis dispatch failure must not change a successful download to failed.
        if workflow.patched("analyze-after-ingest-v1") and result.get("clip_id"):
            await workflow.execute_activity(
                "enqueue_ingested_analysis_activity",
                str(result["clip_id"]),
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=retry_policy,
            )
        if workflow.patched("authorized-trailer-passthrough-v1") and result.get("clip_id"):
            prepared = await workflow.execute_activity(
                "prepare_authorized_passthrough_activity",
                source_id,
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=RetryPolicy(maximum_attempts=2),
                result_type=dict[str, object],
            )
            result = {**result, "post_ingest": prepared}
        return result
