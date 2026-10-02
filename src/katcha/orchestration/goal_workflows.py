from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy


@workflow.defn
class CommandGoalWorkflow:
    @workflow.run
    async def run(self, goal_id: str) -> dict[str, Any]:
        for _ in range(260):
            state = await workflow.execute_activity(
                "advance_command_goal_activity",
                goal_id,
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            if state in {"completed", "blocked", "needs_input", "failed", "cancelled"}:
                return {"goal_id": goal_id, "status": state}
            if state in {"waiting_workflow", "waiting_confirmation"}:
                await workflow.sleep(timedelta(seconds=15))
        workflow.continue_as_new(goal_id)
