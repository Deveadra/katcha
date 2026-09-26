from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy


async def _run_refresh(channel_profile_id: str, run_key: str) -> dict[str, object]:
    retry = RetryPolicy(
        initial_interval=timedelta(seconds=5),
        backoff_coefficient=2.0,
        maximum_interval=timedelta(minutes=2),
        maximum_attempts=3,
    )
    # Start the publishing-queue sync independently. Its provider work is not awaited;
    # an unavailable reach provider or scheduler must not halt channel intelligence.
    try:
        reach_sync = await workflow.execute_activity(
            "schedule_channel_reach_sync_activity",
            args=[channel_profile_id, workflow.now().isoformat()],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=1),

        )
    except Exception:
        reach_sync = {"status": "unavailable", "reason": "reach_sync_scheduling_failed"}
    observations = await workflow.execute_activity(
        "derive_channel_observations_activity",
        channel_profile_id,
        start_to_close_timeout=timedelta(minutes=3),
        retry_policy=retry,

    )
    ranking = await workflow.execute_activity(
        "train_channel_ranking_activity",
        args=[channel_profile_id, run_key],
        start_to_close_timeout=timedelta(minutes=3),
        retry_policy=retry,

    )
    economics = await workflow.execute_activity(
        "compute_channel_economics_activity",
        args=[channel_profile_id, run_key],
        start_to_close_timeout=timedelta(minutes=3),
        retry_policy=retry,

    )
    schedule = await workflow.execute_activity(
        "compute_channel_schedule_activity",
        args=[channel_profile_id, run_key],
        start_to_close_timeout=timedelta(minutes=3),
        retry_policy=retry,

    )
    edit_performance = await workflow.execute_activity(
        "refresh_edit_blueprint_performance_activity",
        args=[channel_profile_id, run_key],
        start_to_close_timeout=timedelta(minutes=5),
        retry_policy=retry,

    )
    packaging_intelligence = await workflow.execute_activity(
        "refresh_packaging_intelligence_activity",
        args=[channel_profile_id, run_key],
        start_to_close_timeout=timedelta(minutes=10),
        retry_policy=retry,

    )
    activation_performance = await workflow.execute_activity(
        "refresh_trend_activation_performance_activity",
        args=[channel_profile_id, run_key],
        start_to_close_timeout=timedelta(minutes=5),
        retry_policy=retry,

    )
    automation = await workflow.execute_activity(
        "apply_channel_safety_demotion_activity",
        channel_profile_id,
        start_to_close_timeout=timedelta(minutes=2),
        retry_policy=retry,

    )
    try:
        packaging_seed = await workflow.execute_activity(
            "seed_channel_packaging_activity",
            channel_profile_id,
            start_to_close_timeout=timedelta(minutes=8),
            retry_policy=RetryPolicy(maximum_attempts=1),

        )
    except Exception:
        packaging_seed = {"status": "unavailable"}
    try:
        packaging_experiments = await workflow.execute_activity(
            "run_channel_packaging_experiments_activity",
            args=[channel_profile_id, workflow.now().isoformat()],
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=RetryPolicy(maximum_attempts=1),

        )
    except Exception:
        packaging_experiments = {"status": "unavailable"}
    return {
        "channel_profile_id": channel_profile_id,
        "run_key": run_key,
        "reach_sync": reach_sync,
        "observations": observations,
        "ranking": ranking,
        "economics": economics,
        "schedule": schedule,
        "edit_performance": edit_performance,
        "packaging_intelligence": packaging_intelligence,
        "packaging_experiments": packaging_experiments,
        "activation_performance": activation_performance,
        "automation": automation,
        "packaging_seed": packaging_seed,
    }


@workflow.defn
class ChannelIntelligenceRefreshWorkflow:
    @workflow.run
    async def run(
        self,
        channel_profile_id: str,
        run_key: str,
    ) -> dict[str, object]:
        return await _run_refresh(channel_profile_id, run_key)


@workflow.defn
class ChannelIntelligenceScheduleWorkflow:
    @workflow.run
    async def run(
        self,
        channel_profile_id: str,
        interval_hours: int = 6,
        cycles_before_continue: int = 120,
    ) -> None:
        if interval_hours < 1:
            raise ValueError("intelligence refresh interval must be at least one hour")
        if cycles_before_continue < 1:
            raise ValueError("cycles_before_continue must be positive")

        for _ in range(cycles_before_continue):
            now = workflow.now()
            run_key = f"scheduled-{now.strftime('%Y%m%dT%H%M%SZ')}"
            await _run_refresh(channel_profile_id, run_key)
            await workflow.sleep(timedelta(hours=interval_hours))

        workflow.continue_as_new(args=[channel_profile_id, interval_hours, cycles_before_continue])


async def _run_trend_activation(
    channel_profile_id: str,
    run_key: str,
) -> dict[str, object]:
    retry = RetryPolicy(
        initial_interval=timedelta(seconds=5),
        backoff_coefficient=2.0,
        maximum_interval=timedelta(minutes=2),
        maximum_attempts=3,
    )
    return await workflow.execute_activity(
        "run_channel_trend_activation_activity",
        args=[channel_profile_id, run_key],
        start_to_close_timeout=timedelta(minutes=5),
        retry_policy=retry,

    )


@workflow.defn
class ChannelTrendActivationWorkflow:
    @workflow.run
    async def run(
        self,
        channel_profile_id: str,
        run_key: str,
    ) -> dict[str, object]:
        return await _run_trend_activation(channel_profile_id, run_key)


@workflow.defn
class ChannelTrendActivationScheduleWorkflow:
    @workflow.run
    async def run(
        self,
        channel_profile_id: str,
        interval_hours: int = 1,
        cycles_before_continue: int = 120,
    ) -> None:
        if interval_hours < 1:
            raise ValueError("trend activation interval must be at least one hour")
        if cycles_before_continue < 1:
            raise ValueError("cycles_before_continue must be positive")

        for _ in range(cycles_before_continue):
            now = workflow.now()
            run_key = f"scheduled-{now.strftime('%Y%m%dT%H%M%SZ')}"
            await _run_trend_activation(channel_profile_id, run_key)
            await workflow.sleep(timedelta(hours=interval_hours))

        workflow.continue_as_new(args=[channel_profile_id, interval_hours, cycles_before_continue])


@workflow.defn
class ChannelTrendActivationPerformanceWorkflow:
    @workflow.run
    async def run(
        self,
        channel_profile_id: str,
        run_key: str,
    ) -> dict[str, object]:
        retry = RetryPolicy(
            initial_interval=timedelta(seconds=5),
            backoff_coefficient=2.0,
            maximum_interval=timedelta(minutes=2),
            maximum_attempts=3,
        )
        return await workflow.execute_activity(
            "refresh_trend_activation_performance_activity",
            args=[channel_profile_id, run_key],
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=retry,

        )
