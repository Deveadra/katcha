"""Browser handoff auth and uncertainty contracts; no live provider calls."""
import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from test_command_goals import receipt
from test_command_goals import saved as saved_fixture

from katcha import db
from katcha.api.main import app
from katcha.goal_models import CommandGoal
from katcha.services import browser_client, goal_tools

saved = saved_fixture


def test_operator_form_values_do_not_enter_goal_history(saved, monkeypatch):
    goal = receipt(saved[0])
    rpc = AsyncMock(return_value={"available": True, "text": "Signed in"})
    monkeypatch.setattr(browser_client, "run_browser", rpc)
    client = TestClient(app)
    response = client.post(f"/v1/ai/goals/{goal.id}/browser",
                           json={"action": "fill", "ref": "e1", "text": "private-password"})
    assert response.status_code == 200
    rpc.assert_awaited_once()
    assert rpc.call_args.args[0].actor == goal.actor
    with db.session_scope() as session:
        current = session.get(CommandGoal, goal.id)
        assert "private-password" not in str(current.observations)
        assert "private-password" not in str(current.request)
    invalid = client.post(f"/v1/ai/goals/{goal.id}/browser",
                          json={"action": "private-password", "text": "private-password"})
    assert invalid.status_code == 422
    assert "private-password" not in invalid.text


def test_operator_cannot_access_another_actors_browser(saved, monkeypatch):
    goal = receipt(saved[0])
    with db.session_scope() as session:
        session.get(CommandGoal, goal.id).actor = "another-actor"
    rpc = AsyncMock()
    monkeypatch.setattr(browser_client, "run_browser", rpc)
    response = TestClient(app).get(f"/v1/ai/goals/{goal.id}/browser")
    assert response.status_code == 403
    rpc.assert_not_awaited()


def test_terminal_goal_browser_can_be_closed_but_not_reopened(saved, monkeypatch):
    goal = receipt(saved[0])
    with db.session_scope() as session:
        session.get(CommandGoal, goal.id).status = "completed"
    rpc = AsyncMock(return_value={"available": False, "code": "session_closed"})
    monkeypatch.setattr(browser_client, "run_browser", rpc)
    client = TestClient(app)
    closed = client.post(f"/v1/ai/goals/{goal.id}/browser", json={"action": "close"})
    assert closed.status_code == 200
    assert client.post(f"/v1/ai/goals/{goal.id}/browser", json={
        "action": "open", "url": "https://example.com"}).status_code == 409
    assert rpc.await_count == 1


async def test_goal_browser_confirmation_and_no_auto_retry(saved, monkeypatch):
    tool = goal_tools.TOOLS["browser_action"]
    assert tool.mutates and tool.confirm and not tool.retry_safe
    assert not goal_tools.TOOLS["browser_snapshot"].mutates
    goal = receipt(saved[0])
    rpc = AsyncMock(return_value={"available": False, "outcome_uncertain": True})
    monkeypatch.setattr(browser_client, "run_browser", rpc)
    result = await goal_tools.run_native_tool(goal, "browser_action", {
        "action": "open", "url": "https://example.com"}, uuid.uuid4())
    assert result["outcome_uncertain"]
    assert rpc.await_count == 1
    with pytest.raises(ValueError, match="screenshots"):
        await goal_tools.run_native_tool(goal, "browser_action", {"action": "screenshot"},
                                         uuid.uuid4())


async def test_unconfigured_browser_returns_actionable_failure(saved, monkeypatch):
    monkeypatch.setattr(browser_client, "get_settings", lambda: saved[1])
    result = await browser_client.run_browser(receipt(saved[0]), {"action": "snapshot"})
    assert result["code"] == "browser_not_configured"
    assert not result["available"]
