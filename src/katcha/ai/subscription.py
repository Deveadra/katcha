"""Subscription-backed structured text for production as well as conversation."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import httpx
from pydantic import BaseModel

from katcha.ai.router import ModelTarget, record_usage
from katcha.config import Settings
from katcha.domain import AITask
from katcha.integrations import chatgpt, codex


@dataclass(frozen=True)
class SubscriptionResult:
    value: BaseModel
    target: ModelTarget
    input_tokens: int
    output_tokens: int


def subscription_connected(settings: Settings) -> bool:
    if not settings.credential_encryption_key:
        return False
    return bool(
        (settings.codex_enabled and codex.connection_status().get("connected"))
        or (settings.chatgpt_host_id and chatgpt.connection_status().get("plan_usage_enabled"))
    )


def generate_subscription_json(
    *,
    prompt: str,
    schema: type[BaseModel],
    task: AITask,
    reference_type: str,
    reference_id: str,
    settings: Settings,
    image_bytes: bytes | None = None,
) -> SubscriptionResult | None:
    if (
        not settings.ai_enabled
        or settings.resolved_ai_execution_mode() != "live"
        or not settings.credential_encryption_key
    ):
        return None
    providers = []
    if settings.codex_enabled and codex.connection_status().get("connected"):
        providers.append(("codex", codex.invoke_json))
    if (image_bytes is None and settings.chatgpt_host_id
            and chatgpt.connection_status().get("plan_usage_enabled")):
        providers.append(("chatgpt", chatgpt.invoke_json))
    errors = []
    for provider, invoke in providers:
        try:
            response = invoke(
                prompt=prompt,
                schema_name=schema.__name__,
                schema=schema.model_json_schema(),
                settings=settings,
                **({"image_bytes": image_bytes} if image_bytes is not None else {}),
            )
            value = schema.model_validate_json(response.text)
        except (
            codex.CodexConnectionError,
            chatgpt.ChatGPTConnectionError,
            httpx.HTTPError,
            ValueError,
        ) as exc:
            errors.append(f"{provider}: {exc}")
            continue
        target = ModelTarget(provider, response.model)
        record_usage(
            task=task,
            target=target,
            input_units=response.input_tokens,
            output_units=response.output_tokens,
            cost_usd=Decimal("0"),
            reference_type=reference_type,
            reference_id=reference_id,
            metadata={"subscription_backed": True, "api_cost_usd": "0"},
        )
        return SubscriptionResult(value, target, response.input_tokens, response.output_tokens)
    if errors and not (settings.openai_api_key or settings.gemini_api_key):
        raise RuntimeError("Subscription generation failed. " + " | ".join(errors)[:1800])
    return None
