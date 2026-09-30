import uuid

from katcha.ai import command_center
from katcha.ai.router import ModelTarget


class LiveSettings:
    ai_enabled = True
    openai_api_key = "test-key"
    gemini_api_key = None

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
    assert "AI answer is unavailable" in result.degraded_reason


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
