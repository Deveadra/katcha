from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from contextlib import suppress

from sqlalchemy import select
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.worker import Worker

from katcha.acquisition.runtime import DISCOVERY_TASK_QUEUE
from katcha.config import get_settings
from katcha.db import session_scope
from katcha.goal_models import CommandGoal
from katcha.intelligence.runtime import INTELLIGENCE_TASK_QUEUE
from katcha.orchestration.client import terminate_workflow_if_running
from katcha.orchestration.discovery_activities import (
    execute_discovery_page_activity,
    finalize_topic_watch_execution_activity,
    mark_discovery_run_failed,
    prepare_command_discovery_candidates_activity,
    prepare_topic_watch_execution_activity,
    record_command_source_prepare_lifecycle_activity,
    record_topic_watch_command_cycle_activity,
)
from katcha.orchestration.discovery_workflows import (
    CommandSourcePrepareWorkflow,
    DiscoveryRunWorkflow,
    TopicWatchScheduleWorkflow,
    TopicWatchWorkflow,
)
from katcha.orchestration.goal_activities import advance_command_goal_activity
from katcha.orchestration.goal_workflows import CommandGoalWorkflow
from katcha.orchestration.intelligence_activities import (
    apply_channel_safety_demotion_activity,
    compute_channel_economics_activity,
    compute_channel_schedule_activity,
    derive_channel_observations_activity,
    record_command_intelligence_workflow_lifecycle_activity,
    refresh_channel_growth_activity,
    refresh_edit_blueprint_performance_activity,
    refresh_packaging_intelligence_activity,
    refresh_trend_activation_performance_activity,
    run_channel_packaging_experiments_activity,
    run_channel_trend_activation_activity,
    run_clip_lifecycle_maintenance_activity,
    schedule_channel_reach_sync_activity,
    seed_channel_packaging_activity,
    train_channel_ranking_activity,
)
from katcha.orchestration.intelligence_workflows import (
    ChannelIntelligenceRefreshWorkflow,
    ChannelIntelligenceScheduleWorkflow,
    ChannelTrendActivationPerformanceWorkflow,
    ChannelTrendActivationScheduleWorkflow,
    ChannelTrendActivationWorkflow,
)
from katcha.orchestration.research_activities import prepare_research_jobs_activity
from katcha.orchestration.research_workflows import AutomaticResearchWorkflow
from katcha.orchestration.trend_activities import (
    refresh_channel_trends_activity,
    refresh_trend_calibration_activity,
)
from katcha.orchestration.trend_workflows import (
    ChannelTrendCalibrationWorkflow,
    ChannelTrendRefreshWorkflow,
)
from katcha.orchestration.worker_group import run_worker_group
from katcha.services.automation_schedules import (
    AutomationScheduleKind,
    list_enabled_automation_schedules,
    locked_schedule_for_reconcile,
)
from katcha.services.ingestion_sources import list_resumable_source_runs
from katcha.services.research import RESEARCH_WORKFLOW_ID
from katcha.trends.runtime import TREND_TASK_QUEUE


async def _resume_persisted_discovery_work(client: Client) -> tuple[int, int]:
    resumed = 0
    present = 0
    for run in list_resumable_source_runs():
        workflow_id = f"discovery-run-{run.id}"
        try:
            await client.start_workflow(
                DiscoveryRunWorkflow.run,
                str(run.id),
                id=workflow_id,
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
                task_queue=DISCOVERY_TASK_QUEUE,
            )
            resumed += 1
        except WorkflowAlreadyStartedError:
            present += 1
    return resumed, present


async def _resume_persisted_command_goals(client: Client) -> tuple[int, int]:
    with session_scope() as session:
        goals = list(
            session.scalars(
                select(CommandGoal).where(
                    CommandGoal.status.in_(
                        ["queued", "running", "waiting_workflow", "waiting_confirmation"]
                    )
                )
            )
        )

    resumed = 0
    present = 0
    for goal in goals:
        workflow_id = f"command-goal-{goal.id}"
        try:
            await client.start_workflow(
                CommandGoalWorkflow.run,
                str(goal.id),
                id=workflow_id,
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
                task_queue=INTELLIGENCE_TASK_QUEUE,
            )
            resumed += 1
        except WorkflowAlreadyStartedError:
            present += 1
    return resumed, present


async def _resume_persisted_automation_schedules(
    client: Client,
) -> tuple[int, int]:
    resumed = 0
    present = 0
    for snapshot in list_enabled_automation_schedules():
        with locked_schedule_for_reconcile(snapshot.id) as row:
            if row.workflow_id != snapshot.workflow_id or not row.enabled:
                continue
            await terminate_workflow_if_running(
                client,
                row.supersedes_workflow_id,
                reason=f"superseded by durable schedule {row.workflow_id}",
            )
            config = dict(row.schedule_config or {})
            if row.schedule_kind == AutomationScheduleKind.CHANNEL_INTELLIGENCE.value:
                workflow_run = ChannelIntelligenceScheduleWorkflow.run
                args = [str(row.subject_id), int(config["interval_hours"]), 120]
                task_queue = INTELLIGENCE_TASK_QUEUE
            elif (
                row.schedule_kind
                == AutomationScheduleKind.CHANNEL_TREND_ACTIVATION.value
            ):
                workflow_run = ChannelTrendActivationScheduleWorkflow.run
                args = [str(row.subject_id), int(config["interval_hours"]), 120]
                task_queue = INTELLIGENCE_TASK_QUEUE
            elif row.schedule_kind == AutomationScheduleKind.TOPIC_WATCH.value:
                workflow_run = TopicWatchScheduleWorkflow.run
                args = [
                    str(row.subject_id),
                    int(config["interval_minutes"]),
                    int(config["top_n"]),
                    0,
                ]
                task_queue = DISCOVERY_TASK_QUEUE
            else:
                raise RuntimeError(
                    "unsupported durable automation schedule kind: "
                    f"{row.schedule_kind}"
                )
            try:
                await client.start_workflow(
                    workflow_run,
                    args=args,
                    id=row.workflow_id,
                    id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
                    task_queue=task_queue,
                )
                resumed += 1
            except WorkflowAlreadyStartedError:
                present += 1
            row.supersedes_workflow_id = None
    return resumed, present

async def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    client = await Client.connect(
        settings.temporal_host,
        namespace=settings.temporal_namespace,
    )
    discovery_resumed, discovery_present = await _resume_persisted_discovery_work(client)
    goal_resumed, goal_present = await _resume_persisted_command_goals(client)
    schedule_resumed, schedule_present = await _resume_persisted_automation_schedules(
        client
    )
    logging.getLogger(__name__).info(
        "intelligence recovery reconciled discovery resumed=%s present=%s; "
        "command goals resumed=%s present=%s; schedules resumed=%s present=%s",
        discovery_resumed,
        discovery_present,
        goal_resumed,
        goal_present,
        schedule_resumed,
        schedule_present,
    )

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as activity_executor:
        discovery_worker = Worker(
            client,
            task_queue=DISCOVERY_TASK_QUEUE,
            workflows=[
                CommandSourcePrepareWorkflow,
                DiscoveryRunWorkflow,
                TopicWatchWorkflow,
                TopicWatchScheduleWorkflow,
                AutomaticResearchWorkflow,
            ],
            activities=[
                execute_discovery_page_activity,
                prepare_command_discovery_candidates_activity,
                record_command_source_prepare_lifecycle_activity,
                mark_discovery_run_failed,
                prepare_topic_watch_execution_activity,
                finalize_topic_watch_execution_activity,
                record_topic_watch_command_cycle_activity,
                prepare_research_jobs_activity,
            ],
            activity_executor=activity_executor,
        )
        trend_worker = Worker(
            client,
            task_queue=TREND_TASK_QUEUE,
            workflows=[
                ChannelTrendRefreshWorkflow,
                ChannelTrendCalibrationWorkflow,
            ],
            activities=[
                refresh_channel_trends_activity,
                refresh_trend_calibration_activity,
            ],
            activity_executor=activity_executor,
        )
        intelligence_worker = Worker(
            client,
            task_queue=INTELLIGENCE_TASK_QUEUE,
            workflows=[
                CommandGoalWorkflow,
                ChannelIntelligenceRefreshWorkflow,
                ChannelIntelligenceScheduleWorkflow,
                ChannelTrendActivationWorkflow,
                ChannelTrendActivationPerformanceWorkflow,
                ChannelTrendActivationScheduleWorkflow,
            ],
            activities=[
                advance_command_goal_activity,
                derive_channel_observations_activity,
                record_command_intelligence_workflow_lifecycle_activity,
                refresh_channel_growth_activity,
                refresh_edit_blueprint_performance_activity,
                refresh_packaging_intelligence_activity,
                run_channel_packaging_experiments_activity,
                run_clip_lifecycle_maintenance_activity,
                seed_channel_packaging_activity,
                train_channel_ranking_activity,
                compute_channel_economics_activity,
                compute_channel_schedule_activity,
                apply_channel_safety_demotion_activity,
                run_channel_trend_activation_activity,
                refresh_trend_activation_performance_activity,
                schedule_channel_reach_sync_activity,
            ],
            activity_executor=activity_executor,
        )
        if settings.research_enabled:
            with suppress(WorkflowAlreadyStartedError):
                await client.start_workflow(
                    AutomaticResearchWorkflow.run,
                    id=RESEARCH_WORKFLOW_ID,
                    task_queue=DISCOVERY_TASK_QUEUE,
                )
        await run_worker_group([discovery_worker, trend_worker, intelligence_worker])


if __name__ == "__main__":
    asyncio.run(main())
