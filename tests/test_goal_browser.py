"""Browser handoff auth and uncertainty contracts; no live provider calls."""
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from test_command_goals import receipt
from test_command_goals import saved as saved_fixture

from katcha import db
from katcha.api.main import app
from katcha.goal_models import CommandGoal
from katcha.services import browser_client, goal_tools, retrieval_diagnostics

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


@pytest.mark.parametrize(
    ("response", "code", "status", "verified"),
    [
        (httpx.Response(200, json={"ready": True, "sessions": 0}),
         "browser_service_ready", "ready", True),
        (httpx.Response(503, json={"detail": "private=secret"}),
         "browser_health_unhealthy", "unhealthy", False),
        (httpx.Response(200, json={"status": "ok"}),
         "browser_health_invalid", "unhealthy", False),
    ],
)
async def test_browser_health_probe_is_live_and_secret_safe(
    monkeypatch, response, code, status, verified
):
    config = SimpleNamespace(
        browser_service_url="http://browser:8010",
        browser_service_token="private-secret",
    )
    monkeypatch.setattr(browser_client, "get_settings", lambda: config)
    original = httpx.AsyncClient
    paths = []

    def client(*args, **kwargs):
        def reply(request):
            paths.append(str(request.url))
            return response

        return original(*args, transport=httpx.MockTransport(reply), **kwargs)

    monkeypatch.setattr(browser_client.httpx, "AsyncClient", client)
    actual = await browser_client.probe_browser_service()
    assert actual["code"] == code
    assert actual["service_status"] == status
    assert actual["live_browser_verified"] is verified
    assert paths == ["http://browser:8010/health"]
    assert "secret" not in str(actual)
    assert "checked_at" in actual


async def test_browser_health_probe_unconfigured_or_unreachable(monkeypatch):
    monkeypatch.setattr(
        browser_client, "get_settings",
        lambda: SimpleNamespace(browser_service_url=None, browser_service_token=None),
    )
    actual = await browser_client.probe_browser_service()
    assert actual["code"] == "browser_not_configured"
    assert actual["live_browser_verified"] is False

    monkeypatch.setattr(
        browser_client, "get_settings",
        lambda: SimpleNamespace(browser_service_url="http://browser:8010",
                                browser_service_token="private-secret"),
    )
    original = httpx.AsyncClient

    def client(*args, **kwargs):
        def fail(request):
            raise httpx.ConnectError("Bearer private-secret", request=request)

        return original(*args, transport=httpx.MockTransport(fail), **kwargs)

    monkeypatch.setattr(browser_client.httpx, "AsyncClient", client)
    actual = await browser_client.probe_browser_service()
    assert actual["code"] == "browser_health_unreachable"
    assert actual["service_status"] == "unreachable"
    assert actual["live_browser_verified"] is False
    assert "secret" not in str(actual)


async def test_retrieval_capabilities_runs_live_probe_not_configuration_only(monkeypatch):
    monkeypatch.setattr(retrieval_diagnostics, "retrieval_capabilities", lambda: {
        "interactive_browser": {"configured": True},
        "media_download": {"live_download_verified": False},
    })
    probe = AsyncMock(return_value={
        "configured": True, "service_status": "ready", "live_browser_verified": True,
    })
    monkeypatch.setattr(retrieval_diagnostics, "probe_browser_service", probe,
                        raising=False)
    # The service uses a local import; patch the actual browser-client function.
    monkeypatch.setattr(browser_client, "probe_browser_service", probe)
    result = await goal_tools.run_read_tool(None, "retrieval_capabilities", {})
    assert result["interactive_browser"]["live_browser_verified"] is True
    assert result["media_download"]["live_download_verified"] is False
    probe.assert_awaited_once()
