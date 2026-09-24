from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy


@workflow.defn
class YouTubeReachSyncWorkflow:
    @workflow.run
    async def run(self, connection_id: str) -> dict[str, object]:
        local_retry = RetryPolicy(
            initial_interval=timedelta(seconds=5),
            backoff_coefficient=2.0,
            maximum_interval=timedelta(minutes=2),
            maximum_attempts=4,
        )
        prepared = await workflow.execute_activity(
            "prepare_reach_reporting_job_activity",
            connection_id,
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=local_retry,
            result_type=dict[str, object],
        )
        provider_job_id = prepared.get("provider_job_id")
        if bool(prepared.get("needs_create")):
            created = await workflow.execute_activity(
                "create_reach_reporting_job_activity",
                args=[connection_id, str(prepared["reach_job_id"])],
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(maximum_attempts=1),
                result_type=dict[str, object],
            )
            provider_job_id = created.get("provider_job_id")
        if not provider_job_id:
            raise RuntimeError("reach reporting workflow has no provider job ID")
        return await workflow.execute_activity(
            "sync_reach_reports_activity",
            args=[
                connection_id,
                str(prepared["reach_job_id"]),
                str(provider_job_id),
            ],
            start_to_close_timeout=timedelta(minutes=10),
            retry_policy=local_retry,
            result_type=dict[str, object],
        )
