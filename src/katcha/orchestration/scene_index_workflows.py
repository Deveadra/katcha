"""One bounded attempt over the canonical measured index; no automatic decode retry."""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy


@workflow.defn
class SceneIndexWorkflow:
    @workflow.run
    async def run(self, context: dict[str, str]) -> None:
        try:
            await workflow.execute_activity(
                "measure_project_media",
                context,
                start_to_close_timeout=timedelta(minutes=3),
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
        except Exception:
            await workflow.execute_activity(
                "fail_scene_index_attempt",
                context,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
