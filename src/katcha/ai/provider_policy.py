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
    configured = settings.agent_provider if heavy else settings.conversation_provider
    preferred = configured
    if preferred == "auto":
        preferred = "codex" if heavy else "gemini"
    secondary = "gemini" if preferred == "codex" else "codex"
    providers = [preferred, secondary, "chatgpt"]
    if settings.allow_paid_openai_fallback:
        providers.append("openai")
    return _ordered(*providers)


def planner_provider_order(settings: Settings) -> tuple[str, ...]:
    preferred = settings.conversation_provider
    if preferred == "auto":
        preferred = "gemini"
    secondary = "gemini" if preferred == "codex" else "codex"
    providers = [preferred, secondary, "chatgpt"]
    if settings.allow_paid_openai_fallback:
        providers.append("openai")
    return _ordered(*providers)
