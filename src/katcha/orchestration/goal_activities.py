import uuid

from temporalio import activity

from katcha.services.goal_runner import advance_goal


@activity.defn
async def advance_command_goal_activity(goal_id: str) -> str:
    return await advance_goal(uuid.UUID(goal_id))
