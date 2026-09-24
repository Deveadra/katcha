from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy


@workflow.defn
class YouTubePackagingActivationWorkflow:
    @workflow.run
    async def run(self, activation_id: str) -> dict[str, object]:
        local_retry = RetryPolicy(
            initial_interval=timedelta(seconds=5),
            backoff_coefficient=2.0,
            maximum_interval=timedelta(minutes=1),
            maximum_attempts=4,
        )
        provider_mutation_once = RetryPolicy(maximum_attempts=1)
        try:
            prepared = await workflow.execute_activity(
                "prepare_packaging_activation_activity",
                activation_id,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=local_retry,
                result_type=dict[str, object],
            )
            if bool(prepared.get("done")):
                return {
                    "activation_id": activation_id,
                    "status": "applied",
                    "reused": True,
                }

            await workflow.execute_activity(
                "apply_packaging_text_activity",
                activation_id,
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=provider_mutation_once,
                result_type=dict[str, object],
            )
            await workflow.execute_activity(
                "apply_packaging_thumbnail_activity",
                activation_id,
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=provider_mutation_once,
                result_type=dict[str, object],
            )
            return await workflow.execute_activity(
                "finalize_packaging_activation_activity",
                activation_id,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=local_retry,
                result_type=dict[str, object],
            )
        except Exception as exc:
            await workflow.execute_activity(
                "mark_packaging_activation_failed",
                args=[activation_id, str(exc)],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            raise
