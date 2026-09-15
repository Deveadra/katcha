from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select

from katcha.config import get_settings
from katcha.db import session_scope
from katcha.domain import AITask
from katcha.models import UsageEvent


@dataclass(frozen=True, slots=True)
class ModelTarget:
    provider: str
    model: str


@dataclass(frozen=True, slots=True)
class ModelRoute:
    primary: ModelTarget
    fallback: ModelTarget | None = None


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


class BudgetExceeded(RuntimeError):
    pass


def route_for(task: AITask) -> ModelRoute:
    return ROUTES[task]


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
        raise BudgetExceeded("AI execution is disabled; set KATCHA_AI_ENABLED=true to enable it")
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
