from __future__ import annotations

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy

from katcha.acquisition.runtime import DISCOVERY_TASK_QUEUE
from katcha.orchestration.discovery_workflows import TopicWatchWorkflow


@workflow.defn
class AutomaticResearchWorkflow:
    """A durable dispatcher; workers can restart without duplicating collections."""

    @workflow.run
    async def run(self) -> None:
        async def collect(job: dict) -> None:
            try:
                await workflow.execute_child_workflow(
                    TopicWatchWorkflow.run,
                    args=[job["topic_watch_id"], job["execution_key"], job["top_n"]],
                    id=job["workflow_id"],
                    task_queue=DISCOVERY_TASK_QUEUE,
                    id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                    execution_timeout=timedelta(minutes=20),
                )
            except Exception as exc:
                # Each discovery run retains its provider error; an unavailable
                # source must not stop the dispatcher or other channels.
                workflow.logger.warning("Research %s: %s", job["workflow_id"], exc)

        for _ in range(120):
            try:
                jobs = await workflow.execute_activity(
                    "prepare_research_jobs_activity",
                    workflow.now().isoformat(),
                    start_to_close_timeout=timedelta(minutes=2),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )
                for offset in range(0, len(jobs), 4):
                    await asyncio.gather(*(collect(job) for job in jobs[offset : offset + 4]))
            except Exception:
                workflow.logger.exception("Research reconciliation failed; retrying next cycle")
            await workflow.sleep(timedelta(minutes=1))
        workflow.continue_as_new()
