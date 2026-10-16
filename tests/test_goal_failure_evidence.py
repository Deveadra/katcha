"""Failure timestamps must survive the goal runner's durable JSON observations."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from test_command_goals import decision, receipt
from test_command_goals import saved as saved_fixture

from katcha import db
from katcha.goal_models import CommandGoalStep
from katcha.production_models import Production
from katcha.render_models import RenderAttempt
from katcha.services import goal_runner
from katcha.services.goal_receipts import get_goal

saved = saved_fixture


@pytest.mark.parametrize("with_render", [False, True])
async def test_failure_observation_persists_then_goal_can_complete(saved, monkeypatch, with_render):
    channel = saved[0]
    stamp = datetime(2026, 10, 16, 6, 30, tzinfo=UTC)
    production_id = uuid.uuid4()
    with db.session_scope() as session:
        session.add(Production(
            id=production_id, clip_id=uuid.uuid4(), channel_profile_id=channel,
            workflow_id=str(uuid.uuid4()), persona_key="host", persona_version="1",
            prompt_version="1", status="failed", stage="render", error="Render failed",
            updated_at=stamp,
        ))
        if with_render:
            session.add(RenderAttempt(
                attempt_key=str(uuid.uuid4()), production_id=production_id,
                channel_profile_id=channel, source_generation=1, attempt_number=1,
                status="failed", stage="render", output_key="fixture/output.mp4",
                manifest_version="1", error="Render failed", updated_at=stamp,
            ))
    goal = receipt(channel, prompt="Inspect why my render failed")
    choices = [decision("failures", mode="inspect"), decision(outcome="complete", mode="inspect")]
    monkeypatch.setattr(goal_runner, "decide_goal", lambda **kwargs: choices.pop(0))
    assert await goal_runner.advance_goal(goal.id) == "running"
    observed = get_goal(goal.id).observations[0]
    assert observed["error"] is None
    evidence = observed["result"]["evidence"][0]
    assert evidence["kind"] == ("render_attempt" if with_render else "production")
    assert datetime.fromisoformat(evidence["updated_at"]).replace(tzinfo=UTC) == stamp
    with db.session_scope() as session:
        step = session.scalar(select(CommandGoalStep).where(CommandGoalStep.goal_id == goal.id))
        assert step.result == observed["result"]
        assert step.error is None
    assert await goal_runner.advance_goal(goal.id) == "completed"
    assert get_goal(goal.id).result["evidence"][0]["updated_at"] == evidence["updated_at"]
