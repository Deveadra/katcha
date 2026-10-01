from __future__ import annotations

from katcha.config import Settings

HEAVY_COMMAND_INTENTS = frozenset(
    {
        "performance_advice",
        "create_content",
        "source_discovery",
    }
)


def _ordered(*providers: str) -> tuple[str, ...]:
    result: list[str] = []
    for provider in providers:
        if provider and provider not in result:
            result.append(provider)
    return tuple(result)


def command_provider_order(settings: Settings, intent: str) -> tuple[str, ...]:
    heavy = intent in HEAVY_COMMAND_INTENTS
    configured = (
        getattr(settings, "agent_provider", "codex")
        if heavy
        else getattr(settings, "conversation_provider", "gemini")
    )
    preferred = configured
    if preferred == "auto":
        preferred = "codex" if heavy else "gemini"
    secondary = "gemini" if preferred == "codex" else "codex"
    providers = [preferred, secondary, "chatgpt"]
    if getattr(settings, "allow_paid_openai_fallback", False):
        providers.append("openai")
    return _ordered(*providers)


def planner_provider_order(settings: Settings, *, phase: str = "interpret") -> tuple[str, ...]:
    if phase == "bind_actions_after_observation":
        return command_provider_order(settings, "create_content")
    preferred = getattr(settings, "conversation_provider", "gemini")
    if preferred == "auto":
        preferred = "gemini"
    secondary = "gemini" if preferred == "codex" else "codex"
    providers = [preferred, secondary, "chatgpt"]
    if getattr(settings, "allow_paid_openai_fallback", False):
        providers.append("openai")
    return _ordered(*providers)
