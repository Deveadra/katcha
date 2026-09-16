from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy


@workflow.defn
class ChannelTrendRefreshWorkflow:
    @workflow.run
    async def run(self, channel_profile_id: str, run_key: str) -> dict[str, object]:
        return await workflow.execute_activity(
            "refresh_channel_trends_activity",
            args=[channel_profile_id, run_key],
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=3),
                backoff_coefficient=2.0,
                maximum_interval=timedelta(seconds=30),
                maximum_attempts=4,
            ),
            result_type=dict[str, object],
        )
