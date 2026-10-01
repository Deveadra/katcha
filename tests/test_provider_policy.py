from katcha.ai.provider_policy import command_provider_order, planner_provider_order
from katcha.config import Settings


def test_default_policy_uses_gemini_for_conversation_and_codex_for_heavy_work() -> None:
    settings = Settings()

    assert planner_provider_order(settings)[:2] == ("gemini", "codex")
    assert command_provider_order(settings, "conversation")[:2] == ("gemini", "codex")
    assert command_provider_order(settings, "channel_status")[:2] == ("gemini", "codex")
    assert command_provider_order(settings, "performance_advice")[:2] == (
        "codex",
        "gemini",
    )
    assert command_provider_order(settings, "source_discovery")[:2] == (
        "codex",
        "gemini",
    )


def test_paid_openai_fallback_is_opt_in() -> None:
    settings = Settings()
    assert "openai" not in planner_provider_order(settings)
    assert "openai" not in command_provider_order(settings, "conversation")

    paid = Settings(allow_paid_openai_fallback=True)
    assert planner_provider_order(paid)[-1] == "openai"
    assert command_provider_order(paid, "performance_advice")[-1] == "openai"


def test_explicit_provider_preferences_are_respected() -> None:
    settings = Settings(
        conversation_provider="codex",
        agent_provider="gemini",
    )

    assert planner_provider_order(settings)[:2] == ("codex", "gemini")
    assert command_provider_order(settings, "conversation")[:2] == ("codex", "gemini")
    assert command_provider_order(settings, "create_content")[:2] == (
        "gemini",
        "codex",
    )
