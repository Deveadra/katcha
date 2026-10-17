"""Exercise real SDK serialization with an offline provider response."""

import json
import uuid
from types import SimpleNamespace

import pytest
from google.genai import _api_client, errors, types

from katcha.ai import goal_planner
from katcha.integrations.codex import CodexConnectionError


@pytest.mark.parametrize("arguments", [{}, {"path": "sources", "value": [{"enabled": True}]}])
@pytest.mark.parametrize("capacity_rejection", [False, True])
def test_gemini_goal_decision_serializes_arbitrary_tool_arguments(
    monkeypatch, arguments, capacity_rejection
):
    settings = SimpleNamespace(
        ai_enabled=True,
        resolved_ai_execution_mode=lambda: "live",
        gemini_api_key="offline-test-key",
    )
    monkeypatch.setattr(goal_planner, "get_settings", lambda: settings)
    monkeypatch.setattr(goal_planner, "planner_provider_order", lambda *a, **k: ["gemini"])
    reservation = uuid.uuid4()
    monkeypatch.setattr(
        goal_planner,
        "route_for_channel",
        lambda *a, **k: SimpleNamespace(reservation_id=reservation),
    )
    monkeypatch.setattr(goal_planner, "assert_mutation_authority", lambda *a, **k: None)
    records = []
    monkeypatch.setattr(goal_planner, "_record", lambda **kwargs: records.append(kwargs))
    monkeypatch.setattr(
        goal_planner,
        "release_budget_reservation",
        lambda *a, **k: pytest.fail("Valid decision must not release a failed reservation"),
    )
    requests = []

    def request(self, http_method, path, request_dict, http_options=None):
        requests.append(request_dict)
        if capacity_rejection and len(requests) == 1:
            raise errors.ServerError(503, {"error": {"message": "high demand", "code": 503}})
        return types.HttpResponse(
            body=json.dumps(
                {
                    "candidates": [
                        {
                            "content": {
                                "role": "model",
                                "parts": [
                                    {
                                        "text": json.dumps(
                                            {
                                                "outcome": "tool",
                                                "reason": "Inspect the requested configuration",
                                                "tool": "inspect_tool",
                                                "arguments": arguments,
                                                "confidence": 0.98,
                                                "mode": "inspect",
                                                "allowed_mutations": [],
                                                "retrieval_targets": [],
                                                "completion_criteria": "Inspect configuration",
                                            }
                                        )
                                    }
                                ],
                            },
                            "finishReason": "STOP",
                        }
                    ],
                    "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 34},
                }
            )
        )

    # Intercept only HTTP: SDK configuration/schema serialization must actually run.
    monkeypatch.setattr(_api_client.BaseApiClient, "request", request)
    result = goal_planner.decide_goal(
        goal_id=uuid.uuid4(),
        channel_id=uuid.uuid4(),
        number=1,
        context={"prompt": "Inspect sources"},
    )
    assert result.arguments == arguments
    assert len(requests) == (2 if capacity_rejection else 1)
    config = requests[0]["generationConfig"]
    assert "responseSchema" not in config
    assert config["responseJsonSchema"]["properties"]["arguments"]["additionalProperties"] is True
    assert records[0]["target"].model == (
        "gemini-3.7-flash" if capacity_rejection else "gemini-3.8-flash"
    )
    assert records[0]["reservation_id"] == reservation
    assert records[0]["input_tokens"] == 12
    assert records[0]["output_tokens"] == 34


def test_provider_failures_are_aggregated_without_secrets(monkeypatch, caplog):
    settings = SimpleNamespace(
        ai_enabled=True,
        resolved_ai_execution_mode=lambda: "live",
        codex_enabled=True,
        chatgpt_host_id="test",
    )
    monkeypatch.setattr(goal_planner, "get_settings", lambda: settings)
    monkeypatch.setattr(
        goal_planner, "planner_provider_order", lambda *a, **k: ["codex", "chatgpt"]
    )

    def codex(**kwargs):
        raise CodexConnectionError("Codex token refresh failed (HTTP 401): secret-token")

    def chatgpt(**kwargs):
        raise ValueError("Schema failed with secret-token")

    monkeypatch.setattr(goal_planner, "codex_json", codex)
    monkeypatch.setattr(goal_planner, "chatgpt_json", chatgpt)
    monkeypatch.setattr(goal_planner, "release_budget_reservation", lambda *a, **k: None)
    with pytest.raises(ValueError) as failure:
        goal_planner.decide_goal(
            goal_id=uuid.uuid4(), channel_id=uuid.uuid4(), number=1, context={}
        )
    message = str(failure.value)
    assert "codex: authentication or access; reconnect the provider" in message
    assert "chatgpt: invalid request or response" in message
    assert "secret-token" not in message + caplog.text
    assert failure.value.__suppress_context__


@pytest.mark.parametrize(
    ("failure", "category"),
    [
        (goal_planner.BudgetExceeded("secret"), "budget limit"),
        (CodexConnectionError("Codex request failed (HTTP 429): secret"), "rate limit"),
        (TimeoutError("secret"), "connection failure"),
        (RuntimeError("secret"), "provider unavailable"),
    ],
)
def test_safe_failure_categories(failure, category):
    assert goal_planner._failure_category(failure) == category


def test_provider_must_explicitly_classify_original_request():
    from pydantic import ValidationError

    incomplete = {"outcome": "tool", "reason": "Read the library", "confidence": 0.95}
    # Older saved receipts remain readable, but omission from a fresh response is invalid.
    assert goal_planner.GoalDecision.model_validate(incomplete).mode == "inspect"
    with pytest.raises(ValidationError):
        goal_planner.PlannedGoalDecision.model_validate(incomplete)
    required = goal_planner.PlannedGoalDecision.model_json_schema()["required"]
    assert {"mode", "allowed_mutations", "completion_criteria", "retrieval_targets"} <= (
        set(required)
    )


def test_goal_planning_supports_stronger_model_without_changing_chat_default():
    from katcha.ai.router import ROUTES, ModelTarget, _eligible_targets
    from katcha.domain import AITask

    assert ROUTES[AITask.COMMAND_PLANNING].fallback.model == "gemini-3.5-flash-lite"
    assert ModelTarget("gemini", "gemini-3.8-flash") in _eligible_targets(
        AITask.COMMAND_PLANNING, floor=2, providers={"gemini"}
    )
