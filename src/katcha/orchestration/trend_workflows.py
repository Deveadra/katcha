from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy


def _retry_policy() -> RetryPolicy:
    return RetryPolicy(
        initial_interval=timedelta(seconds=3),
        backoff_coefficient=2.0,
        maximum_interval=timedelta(seconds=30),
        maximum_attempts=4,
    )


async def _poll_source(source_id: str, run_key: str) -> dict[str, object]:
    return await workflow.execute_activity(
        "poll_trend_source_activity",
        args=[source_id, run_key],
        start_to_close_timeout=timedelta(minutes=3),
        retry_policy=_retry_policy(),
        result_type=dict[str, object],
    )


@workflow.defn
class ChannelTrendRefreshWorkflow:
    @workflow.run
    async def run(self, channel_profile_id: str, run_key: str) -> dict[str, object]:
        return await workflow.execute_activity(
            "refresh_channel_trends_activity",
            args=[channel_profile_id, run_key],
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=_retry_policy(),
            result_type=dict[str, object],
        )


@workflow.defn
class TrendSourcePollWorkflow:
    @workflow.run
    async def run(self, source_id: str, run_key: str) -> dict[str, object]:
        return await _poll_source(source_id, run_key)


@workflow.defn
class TrendSourceScheduleWorkflow:
    @workflow.run
    async def run(
        self,
        source_id: str,
        interval_seconds: int = 900,
        cycles_before_continue: int = 120,
    ) -> None:
        if interval_seconds < 60:
            raise ValueError("trend source interval must be at least 60 seconds")
        if cycles_before_continue < 1:
            raise ValueError("cycles_before_continue must be positive")

        for _ in range(cycles_before_continue):
            now = workflow.now()
            run_key = f"scheduled-{now.strftime('%Y%m%dT%H%M%SZ')}"
            await _poll_source(source_id, run_key)
            await workflow.sleep(timedelta(seconds=interval_seconds))

        workflow.continue_as_new(
            args=[source_id, interval_seconds, cycles_before_continue]
        )
