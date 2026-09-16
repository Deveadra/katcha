from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select

from katcha.config import get_settings
from katcha.db import session_scope
from katcha.domain import AITask, ChannelStatus
from katcha.intelligence_models import (
    AIBudgetReservation,
    ChannelProfile,
)
from katcha.models import DomainEvent, UsageEvent
from katcha.services.channel_economics import budget_state
from katcha.services.channel_profiles import active_strategy


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
    reservation_id: uuid.UUID | None = None
    reservation_key: str | None = None


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
    AITask.TTS: ModelRoute(
        primary=ModelTarget("openai", "gpt-4o-mini-tts-2025-12-15"),
        fallback=ModelTarget("gemini", "gemini-3.1-flash-tts-preview"),
    ),
}

_MODEL_QUALITY: dict[tuple[str, str], int] = {
    ("openai", "gpt-5.6-luna"): 1,
    ("gemini", "gemini-3.5-flash-lite"): 1,
    ("openai", "gpt-5.6-terra"): 2,
    ("gemini", "gemini-3.8-flash"): 2,
    ("openai", "gpt-5.6-sol"): 3,
    ("openai", "gpt-4o-mini-tts-2025-12-15"): 2,
    ("gemini", "gemini-3.1-flash-tts-preview"): 2,
}

_MODEL_COST_WEIGHT: dict[tuple[str, str], float] = {
    ("openai", "gpt-5.6-luna"): 1.0,
    ("gemini", "gemini-3.5-flash-lite"): 1.4,
    ("gemini", "gemini-3.8-flash"): 3.0,
    ("openai", "gpt-5.6-terra"): 6.0,
    ("openai", "gpt-5.6-sol"): 10.0,
    ("openai", "gpt-4o-mini-tts-2025-12-15"): 1.0,
    ("gemini", "gemini-3.1-flash-tts-preview"): 1.3,
}

_TASK_DEFAULT_FLOOR: dict[AITask, int] = {
    AITask.BULK_VISION: 1,
    AITask.DEEP_VIDEO: 2,
    AITask.SHORT_SCRIPT: 2,
    AITask.LONGFORM_EDITOR: 2,
    AITask.LONGFORM_CRITIC: 2,
    AITask.METADATA: 1,
    AITask.PERFORMANCE_ANALYSIS: 2,
    AITask.TTS: 2,
}


class BudgetExceeded(RuntimeError):
    pass


class ChannelBudgetExceeded(BudgetExceeded):
    pass


class BudgetReservationSettled(BudgetExceeded):
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


def _expire_reservations(
    session: object,
    channel_profile_id: uuid.UUID,
    now: datetime,
) -> None:
    rows = list(
        session.scalars(
            select(AIBudgetReservation).where(
                AIBudgetReservation.channel_profile_id == channel_profile_id,
                AIBudgetReservation.status == "reserved",
                AIBudgetReservation.expires_at <= now,
            )
        )
    )
    for row in rows:
        row.status = "expired"
        row.settled_at = now


def route_for_channel(
    task: AITask,
    channel_profile_id: uuid.UUID,
    *,
    estimated_increment_usd: Decimal,
    expected_value: float = 0.5,
    reference_type: str | None = None,
    reference_id: str | None = None,
    reservation_key: str | None = None,
    reservation_ttl_minutes: int = 30,
    preferred_target: ModelTarget | None = None,
) -> RoutingDecision:
    if estimated_increment_usd < 0:
        raise ValueError("estimated_increment_usd cannot be negative")
    if expected_value < 0 or expected_value > 1:
        raise ValueError("expected_value must be between 0 and 1")
    if reservation_ttl_minutes < 1 or reservation_ttl_minutes > 240:
        raise ValueError("reservation_ttl_minutes must be between 1 and 240")
    providers = _available_providers()
    if not providers:
        raise BudgetExceeded("no configured AI provider is available")

    now = datetime.now(UTC)
    key = reservation_key or f"{task.value}-{uuid.uuid4().hex}"
    with session_scope() as session:
        profile = session.scalar(
            select(ChannelProfile)
            .where(ChannelProfile.id == channel_profile_id)
            .with_for_update()
        )
        if profile is None:
            raise ValueError(f"channel profile not found: {channel_profile_id}")
        if profile.status != ChannelStatus.ACTIVE.value:
            raise ValueError("channel profile is not active")
        _expire_reservations(session, profile.id, now)

        existing = session.scalar(
            select(AIBudgetReservation).where(
                AIBudgetReservation.channel_profile_id == profile.id,
                AIBudgetReservation.reservation_key == key,
            )
        )
        if existing is not None and existing.status == "settled":
            raise BudgetReservationSettled(
                f"budget reservation already settled: {key}"
            )
        if existing is not None and existing.status == "reserved":
            metadata = dict(existing.reservation_metadata or {})
            primary = ModelTarget(
                str(metadata["provider"]),
                str(metadata["model"]),
            )
            fallback = None
            if metadata.get("fallback_provider") and metadata.get("fallback_model"):
                fallback = ModelTarget(
                    str(metadata["fallback_provider"]),
                    str(metadata["fallback_model"]),
                )
            return RoutingDecision(
                route=ModelRoute(primary=primary, fallback=fallback),
                reason=str(metadata.get("reason") or "reserved_retry"),
                strategy_version=int(metadata.get("strategy_version") or 1),
                budget_headroom_usd=Decimal(
                    str(metadata.get("budget_headroom_usd") or "0")
                ),
                expected_value=float(metadata.get("expected_value") or expected_value),
                quality_floor=int(metadata.get("quality_floor") or 1),
                reservation_id=existing.id,
                reservation_key=key,
            )

        strategy = active_strategy(session, profile)
        state = budget_state(session, profile, now=now)
        headroom = Decimal(str(state["budget_headroom_usd"]))
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
        if preferred_target is not None and preferred_target not in candidates:
            raise BudgetExceeded(
                "pinned model target is unavailable or below the configured quality floor: "
                f"{preferred_target.provider}/{preferred_target.model}"
            )

        mode = str(policy.get("mode") or "balanced").casefold()
        effective_ceiling = Decimal(str(state["effective_budget_usd"]))
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
        if preferred_target is not None:
            chosen = preferred_target
            reason = "pinned_target"
        elif mode == "quality" and primary_available:
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
        fallback = None if preferred_target is not None else (remaining[0] if remaining else None)
        reservation = existing or AIBudgetReservation(
            channel_profile_id=profile.id,
            reservation_key=key,
            task=task.value,
            reference_type=reference_type,
            reference_id=reference_id,
            estimated_cost_usd=estimated_increment_usd,
            status="reserved",
            expires_at=now + timedelta(minutes=reservation_ttl_minutes),
            reservation_metadata={},
        )
        reservation.task = task.value
        reservation.reference_type = reference_type
        reservation.reference_id = reference_id
        reservation.estimated_cost_usd = estimated_increment_usd
        reservation.actual_cost_usd = None
        reservation.status = "reserved"
        reservation.expires_at = now + timedelta(minutes=reservation_ttl_minutes)
        reservation.settled_at = None
        reservation.reservation_metadata = {
            "provider": chosen.provider,
            "model": chosen.model,
            "fallback_provider": fallback.provider if fallback else None,
            "fallback_model": fallback.model if fallback else None,
            "reason": reason,
            "strategy_version": strategy.version,
            "expected_value": expected_value,
            "quality_floor": floor,
            "budget_headroom_usd": str(headroom),
        }
        session.add(reservation)
        session.flush()
        session.add(
            DomainEvent(
                aggregate_type="channel_profile",
                aggregate_id=str(profile.id),
                event_type="channel_profile.ai_budget_reserved",
                payload={
                    "channel_profile_id": str(profile.id),
                    "reservation_id": str(reservation.id),
                    "reservation_key": key,
                    "task": task.value,
                    "provider": chosen.provider,
                    "model": chosen.model,
                    "fallback_provider": fallback.provider if fallback else None,
                    "fallback_model": fallback.model if fallback else None,
                    "reason": reason,
                    "strategy_version": strategy.version,
                    "expected_value": expected_value,
                    "quality_floor": floor,
                    "estimated_increment_usd": str(estimated_increment_usd),
                    "budget_headroom_usd": str(headroom),
                },
            )
        )
        return RoutingDecision(
            route=ModelRoute(primary=chosen, fallback=fallback),
            reason=reason,
            strategy_version=strategy.version,
            budget_headroom_usd=headroom,
            expected_value=expected_value,
            quality_floor=floor,
            reservation_id=reservation.id,
            reservation_key=key,
        )


def release_budget_reservation(
    reservation_id: uuid.UUID | None,
    *,
    reason: str,
) -> None:
    if reservation_id is None:
        return
    with session_scope() as session:
        reservation = session.scalar(
            select(AIBudgetReservation)
            .where(AIBudgetReservation.id == reservation_id)
            .with_for_update()
        )
        if reservation is None or reservation.status != "reserved":
            return
        reservation.status = "released"
        reservation.settled_at = datetime.now(UTC)
        reservation.reservation_metadata = {
            **dict(reservation.reservation_metadata or {}),
            "release_reason": reason[:500],
        }


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
    reservation_id: uuid.UUID | None = None,
) -> None:
    with session_scope() as session:
        reservation = None
        if reservation_id is not None:
            reservation = session.scalar(
                select(AIBudgetReservation)
                .where(AIBudgetReservation.id == reservation_id)
                .with_for_update()
            )
            if reservation is None:
                raise RuntimeError("AI budget reservation disappeared before settlement")
            if reservation.status == "settled":
                return
            if reservation.status != "reserved":
                raise RuntimeError(
                    f"AI budget reservation is not active: {reservation.status}"
                )
            if reservation.task != task.value:
                raise RuntimeError("AI budget reservation task does not match usage")

        usage_metadata = dict(metadata or {})
        if reservation is not None:
            usage_metadata["budget_reservation_id"] = str(reservation.id)
            usage_metadata["budget_reservation_key"] = reservation.reservation_key
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
                usage_metadata=usage_metadata,
            )
        )
        if reservation is not None:
            reservation.actual_cost_usd = cost_usd
            reservation.status = "settled"
            reservation.settled_at = datetime.now(UTC)
            reservation.reservation_metadata = {
                **dict(reservation.reservation_metadata or {}),
                "actual_provider": target.provider,
                "actual_model": target.model,
                "actual_cost_usd": str(cost_usd),
            }
