from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select

from katcha.config import get_settings
from katcha.db import session_scope
from katcha.domain import AITask
from katcha.models import DomainEvent, UsageEvent
from katcha.services.channel_economics import latest_economics_snapshot
from katcha.services.channel_profiles import active_strategy, ensure_active_profile


@dataclass(frozen=True, slots=True)
class ModelTarget:
    provider: str
    model: str


@dataclass(frozen=True, slots=True)
class ModelRoute:
    primary: ModelTarget
    fallback: ModelTarget | None = None


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    route: ModelRoute
    reason: str
    strategy_version: int
    budget_headroom_usd: Decimal
    expected_value: float
    quality_floor: int


ROUTES: dict[AITask, ModelRoute] = {
    AITask.BULK_VISION: ModelRoute(
        primary=ModelTarget("openai", "gpt-5.6-luna"),
        fallback=ModelTarget("gemini", "gemini-3.5-flash-lite"),
    ),
    AITask.DEEP_VIDEO: ModelRoute(
        primary=ModelTarget("gemini", "gemini-3.8-flash"),
        fallback=ModelTarget("openai", "gpt-5.6-terra"),
    ),
    AITask.SHORT_SCRIPT: ModelRoute(
        primary=ModelTarget("openai", "gpt-5.6-terra"),
        fallback=ModelTarget("gemini", "gemini-3.8-flash"),
    ),
    AITask.LONGFORM_EDITOR: ModelRoute(
        primary=ModelTarget("openai", "gpt-5.6-sol"),
        fallback=ModelTarget("gemini", "gemini-3.8-flash"),
    ),
    AITask.LONGFORM_CRITIC: ModelRoute(
        primary=ModelTarget("gemini", "gemini-3.8-flash"),
        fallback=ModelTarget("openai", "gpt-5.6-sol"),
    ),
    AITask.METADATA: ModelRoute(
        primary=ModelTarget("openai", "gpt-5.6-luna"),
        fallback=ModelTarget("gemini", "gemini-3.5-flash-lite"),
    ),
    AITask.PERFORMANCE_ANALYSIS: ModelRoute(
        primary=ModelTarget("openai", "gpt-5.6-sol"),
        fallback=ModelTarget("gemini", "gemini-3.8-flash"),
    ),
}

_MODEL_QUALITY: dict[tuple[str, str], int] = {
    ("openai", "gpt-5.6-luna"): 1,
    ("gemini", "gemini-3.5-flash-lite"): 1,
    ("openai", "gpt-5.6-terra"): 2,
    ("gemini", "gemini-3.8-flash"): 2,
    ("openai", "gpt-5.6-sol"): 3,
}

_MODEL_COST_WEIGHT: dict[tuple[str, str], float] = {
    ("openai", "gpt-5.6-luna"): 1.0,
    ("gemini", "gemini-3.5-flash-lite"): 1.4,
    ("gemini", "gemini-3.8-flash"): 3.0,
    ("openai", "gpt-5.6-terra"): 6.0,
    ("openai", "gpt-5.6-sol"): 10.0,
}

_TASK_DEFAULT_FLOOR: dict[AITask, int] = {
    AITask.BULK_VISION: 1,
    AITask.DEEP_VIDEO: 2,
    AITask.SHORT_SCRIPT: 2,
    AITask.LONGFORM_EDITOR: 2,
    AITask.LONGFORM_CRITIC: 2,
    AITask.METADATA: 1,
    AITask.PERFORMANCE_ANALYSIS: 2,
}


class BudgetExceeded(RuntimeError):
    pass


class ChannelBudgetExceeded(BudgetExceeded):
    pass


def route_for(task: AITask) -> ModelRoute:
    return ROUTES[task]


def _available_providers() -> set[str]:
    settings = get_settings()
    providers: set[str] = set()
    if settings.openai_api_key:
        providers.add("openai")
    if settings.gemini_api_key:
        providers.add("gemini")
    return providers


def _quality_floor(task: AITask, policy: dict[str, object]) -> int:
    default = _TASK_DEFAULT_FLOOR.get(task, 1)
    task_floors = policy.get("task_quality_floors")
    if isinstance(task_floors, dict):
        raw = task_floors.get(task.value)
        if raw is not None:
            try:
                return max(1, min(3, int(raw)))
            except (TypeError, ValueError):
                pass
    raw_floor = policy.get("quality_floor")
    if isinstance(raw_floor, int):
        return max(1, min(3, raw_floor))
    return default


def _eligible_targets(
    task: AITask,
    *,
    floor: int,
    providers: set[str],
) -> list[ModelTarget]:
    route = ROUTES[task]
    candidates = [route.primary]
    if route.fallback is not None:
        candidates.append(route.fallback)
    return [
        target
        for target in candidates
        if target.provider in providers
        and _MODEL_QUALITY.get((target.provider, target.model), 0) >= floor
    ]


def route_for_channel(
    task: AITask,
    channel_profile_id: uuid.UUID,
    *,
    estimated_increment_usd: Decimal,
    expected_value: float = 0.5,
) -> RoutingDecision:
    if expected_value < 0 or expected_value > 1:
        raise ValueError("expected_value must be between 0 and 1")
    providers = _available_providers()
    if not providers:
        raise BudgetExceeded("no configured AI provider is available")

    with session_scope() as session:
        profile = ensure_active_profile(session, channel_profile_id)
        strategy = active_strategy(session, profile)
        economics = latest_economics_snapshot(session, profile)
        headroom = (
            economics.budget_headroom_usd
            if economics is not None
            else strategy.monthly_hard_budget_usd
        )
        if estimated_increment_usd > headroom:
            raise ChannelBudgetExceeded(
                "channel AI budget headroom is insufficient: "
                f"need ${estimated_increment_usd:.4f}, have ${headroom:.4f}"
            )

        policy = dict(strategy.routing_policy or {})
        floor = _quality_floor(task, policy)
        candidates = _eligible_targets(task, floor=floor, providers=providers)
        if not candidates:
            raise BudgetExceeded(
                f"no configured provider satisfies quality floor {floor} for {task.value}"
            )

        mode = str(policy.get("mode") or "balanced").casefold()
        effective_ceiling = strategy.monthly_hard_budget_usd
        if economics is not None:
            effective_ceiling += economics.reinvestable_usd
        headroom_ratio = (
            float(headroom / effective_ceiling)
            if effective_ceiling > 0
            else 0.0
        )
        static_primary = ROUTES[task].primary
        primary_available = static_primary in candidates
        conserve = (
            mode == "economy"
            or expected_value < 0.35
            or headroom_ratio < 0.20
        )
        if mode == "quality" and primary_available:
            chosen = static_primary
            reason = "quality_preferred"
        elif conserve:
            chosen = min(
                candidates,
                key=lambda item: _MODEL_COST_WEIGHT.get(
                    (item.provider, item.model),
                    100.0,
                ),
            )
            reason = "budget_conserve"
        elif primary_available:
            chosen = static_primary
            reason = "task_primary"
        else:
            chosen = candidates[0]
            reason = "primary_provider_unavailable"

        remaining = [item for item in candidates if item != chosen]
        fallback = remaining[0] if remaining else None
        decision = RoutingDecision(
            route=ModelRoute(primary=chosen, fallback=fallback),
            reason=reason,
            strategy_version=strategy.version,
            budget_headroom_usd=headroom,
            expected_value=expected_value,
            quality_floor=floor,
        )
        session.add(
            DomainEvent(
                aggregate_type="channel_profile",
                aggregate_id=str(profile.id),
                event_type="channel_profile.ai_routed",
                payload={
                    "channel_profile_id": str(profile.id),
                    "task": task.value,
                    "provider": chosen.provider,
                    "model": chosen.model,
                    "fallback_provider": fallback.provider if fallback else None,
                    "fallback_model": fallback.model if fallback else None,
                    "reason": reason,
                    "strategy_version": strategy.version,
                    "expected_value": expected_value,
                    "quality_floor": floor,
                    "budget_headroom_usd": str(headroom),
                },
            )
        )
        return decision


def month_to_date_cost() -> Decimal:
    now = datetime.now(UTC)
    month_start = datetime(now.year, now.month, 1, tzinfo=UTC)
    with session_scope() as session:
        value = session.scalar(
            select(func.coalesce(func.sum(UsageEvent.cost_usd), 0)).where(
                UsageEvent.created_at >= month_start
            )
        )
    return Decimal(str(value or 0))


def assert_ai_budget(estimated_increment_usd: Decimal = Decimal("0")) -> None:
    settings = get_settings()
    if not settings.ai_enabled:
        raise BudgetExceeded(
            "AI execution is disabled; set KATCHA_AI_ENABLED=true to enable it"
        )
    projected = month_to_date_cost() + estimated_increment_usd
    limit = Decimal(str(settings.ai_budget_usd_monthly))
    if projected > limit:
        raise BudgetExceeded(
            f"AI monthly budget exceeded: projected ${projected:.4f} > limit ${limit:.2f}"
        )


def record_usage(
    *,
    task: AITask,
    target: ModelTarget,
    input_units: int,
    output_units: int,
    cost_usd: Decimal,
    reference_type: str | None = None,
    reference_id: str | None = None,
    metadata: dict[str, object] | None = None,
) -> None:
    with session_scope() as session:
        session.add(
            UsageEvent(
                task=task.value,
                provider=target.provider,
                model=target.model,
                input_units=input_units,
                output_units=output_units,
                cost_usd=cost_usd,
                reference_type=reference_type,
                reference_id=reference_id,
                usage_metadata=metadata or {},
            )
        )
