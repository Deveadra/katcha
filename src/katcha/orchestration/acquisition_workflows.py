"""Durable remote acquisition waiting; provider execution stays on the remote host."""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from katcha.orchestration.resource_queue import execute_resource_activity


@workflow.defn
class RemoteAcquisitionWorkflow:
    @workflow.run
    async def run(self, channel_id: str, job_id: str) -> dict:
        for _ in range(500):
            result = await execute_resource_activity(
                "reconcile_remote_acquisition",
                args=[channel_id, job_id],
                start_to_close_timeout=timedelta(hours=2),
                heartbeat_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(maximum_attempts=5),
            )
            if result["status"] in {"completed", "failed", "cancelled", "recovery_required"}:
                return result
            await workflow.sleep(20)
        workflow.continue_as_new(args=[channel_id, job_id])


async def wait_for_remote_source(source_id: str, *, task_queue: str | None = None):
    from temporalio.exceptions import ApplicationError

    result = await execute_resource_activity(
        "remote_source_acquisition",
        source_id,
        task_queue=task_queue,
        start_to_close_timeout=timedelta(seconds=30),
        retry_policy=RetryPolicy(maximum_attempts=3),
    )
    if not result.get("job_id"):
        return
    outcome = await workflow.execute_child_workflow(
        "RemoteAcquisitionWorkflow",
        args=[result["channel_id"], result["job_id"]],
        task_queue=task_queue,
        id=f"{workflow.info().workflow_id}:remote:{result['job_id']}",
    )
    if outcome["status"] != "completed":
        raise ApplicationError(
            "Remote acquisition needs attention; inspect the saved acquisition job",
            type="RemoteAcquisitionBlocked",
            non_retryable=True,
        )
