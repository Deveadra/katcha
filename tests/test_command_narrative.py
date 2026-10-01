import uuid
from decimal import Decimal

from katcha.ai import command_center
from katcha.ai.router import ModelTarget
from katcha.domain import AITask


class LiveSettings:
    ai_enabled = True
    openai_api_key = "test-key"
    gemini_api_key = None
    conversation_provider = "auto"
    agent_provider = "auto"
    allow_paid_openai_fallback = True
    codex_enabled = False
    chatgpt_host_id = None

    @staticmethod
    def resolved_ai_execution_mode():
        return "live"


def test_live_narrative_failure_is_labeled_as_saved_data(monkeypatch):
    target = ModelTarget("openai", "test-model")
    decision = type("Decision", (), {
        "route": type("Route", (), {"primary": target, "fallback": None})(),
        "reservation_id": None,
    })()
    monkeypatch.setattr(command_center, "route_for_channel", lambda *args, **kwargs: decision)
    monkeypatch.setattr(
        command_center, "_openai",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("provider offline")),
    )
    result = command_center.compose_grounded_answer(
        channel_profile_id=uuid.uuid4(), request_id=uuid.uuid4(),
        user_prompt="What failed?", intent="failures",
        deterministic_answer="A stored render failed.", evidence=[],
        settings=LiveSettings(),  # type: ignore[arg-type]
    )
    assert result.value.answer == "A stored render failed."
    assert result.target.provider == "katcha"
    assert "Live AI could not complete this answer" in result.degraded_reason
    assert "openai/test-model" in result.degraded_reason


def test_openai_answer_has_room_for_reasoning_and_structured_text(monkeypatch):
    import json
    import sys
    from types import SimpleNamespace

    captured = {}

    def create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            output_text=json.dumps({"answer": "Hello. What would you like to work on?"}),
            usage=SimpleNamespace(input_tokens=20, output_tokens=40),
        )

    def client(**kwargs):
        assert kwargs["timeout"] == 30.0
        assert kwargs["max_retries"] == 1
        return SimpleNamespace(responses=SimpleNamespace(create=create))

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=client))
    monkeypatch.setattr(command_center, "_record", lambda **kwargs: None)
    result = command_center._openai(
        "Hello", target=ModelTarget("openai", "test-model"),
        settings=LiveSettings(), request_id=uuid.uuid4(), reservation_id=None,
    )
    assert result.value.answer.startswith("Hello")
    assert captured["max_output_tokens"] >= 2000
    assert captured["text"]["format"]["type"] == "json_schema"


class DualLiveSettings(LiveSettings):
    gemini_api_key = "test-gemini-key"


class ProviderRejected(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__("provider rejected request")
        self.status_code = status_code


def test_command_narration_uses_low_cost_command_route(monkeypatch):
    target = ModelTarget("openai", "gpt-5.6-luna")
    captured = {}

    def route(task, channel_profile_id, **kwargs):
        captured["task"] = task
        captured["estimated_increment_usd"] = kwargs["estimated_increment_usd"]
        return type(
            "Decision",
            (),
            {
                "route": type(
                    "Route",
                    (),
                    {"primary": target, "fallback": None},
                )(),
                "reservation_id": None,
            },
        )()

    monkeypatch.setattr(command_center, "route_for_channel", route)
    monkeypatch.setattr(
        command_center,
        "_openai",
        lambda *args, **kwargs: command_center.CommandNarrativeResult(
            command_center.CommandNarrative(answer="Live answer"),
            target,
            12,
            8,
        ),
    )

    result = command_center.compose_grounded_answer(
        channel_profile_id=uuid.uuid4(),
        request_id=uuid.uuid4(),
        user_prompt="What failed?",
        intent="failures",
        deterministic_answer="A stored render failed.",
        evidence=[],
        settings=LiveSettings(),  # type: ignore[arg-type]
    )

    assert result.value.answer == "Live answer"
    assert captured["task"] == AITask.COMMAND_PLANNING
    assert captured["estimated_increment_usd"] == Decimal("0.01")


def test_command_narration_fails_over_on_provider_bad_request(monkeypatch):
    gemini = ModelTarget("gemini", "gemini-3.5-flash-lite")
    openai = ModelTarget("openai", "gpt-5.6-luna")
    decision = type(
        "Decision",
        (),
        {
            "route": type(
                "Route",
                (),
                {"primary": gemini, "fallback": openai},
            )(),
            "reservation_id": None,
        },
    )()
    attempts = []

    monkeypatch.setattr(
        command_center,
        "route_for_channel",
        lambda *args, **kwargs: decision,
    )
    monkeypatch.setattr(
        command_center,
        "_gemini",
        lambda *args, **kwargs: (
            attempts.append("gemini"),
            (_ for _ in ()).throw(ProviderRejected(400)),
        )[1],
    )
    monkeypatch.setattr(
        command_center,
        "_openai",
        lambda *args, **kwargs: (
            attempts.append("openai"),
            command_center.CommandNarrativeResult(
                command_center.CommandNarrative(answer="Recovered live answer"),
                openai,
                10,
                5,
            ),
        )[1],
    )

    result = command_center.compose_grounded_answer(
        channel_profile_id=uuid.uuid4(),
        request_id=uuid.uuid4(),
        user_prompt="Find new sources.",
        intent="source_discovery",
        deterministic_answer="Stored source summary.",
        evidence=[],
        settings=DualLiveSettings(),  # type: ignore[arg-type]
    )

    assert attempts == ["gemini", "openai"]
    assert result.value.answer == "Recovered live answer"
    assert result.target == openai
    assert result.degraded_reason is None
