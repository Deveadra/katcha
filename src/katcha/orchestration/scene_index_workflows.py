"""One bounded attempt over the canonical measured index; no automatic decode retry."""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from katcha.orchestration.resource_queue import execute_resource_activity


@workflow.defn
class SceneIndexWorkflow:
    @workflow.run
    async def run(self, context: dict[str, str]) -> None:
        try:
            await execute_resource_activity(
                "measure_project_media",
                context,
                start_to_close_timeout=timedelta(minutes=3),
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
        except Exception:
            await execute_resource_activity(
                "fail_scene_index_attempt",
                context,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
