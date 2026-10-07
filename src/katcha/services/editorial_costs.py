from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import func, select

from katcha.editorial_models import EditorialRun
from katcha.models import UsageEvent


def editorial_project_usage_cost(session: object, project_id: uuid.UUID) -> Decimal:
    """Sum persisted provider charges attributable to one Editorial project."""
    run_ids = [
        str(value)
        for value in session.scalars(
            select(EditorialRun.id).where(EditorialRun.project_id == project_id)
        )
    ]
    project_cost = session.scalar(
        select(func.coalesce(func.sum(UsageEvent.cost_usd), 0)).where(
            UsageEvent.reference_type == "editorial_project",
            UsageEvent.reference_id == str(project_id),
        )
    )
    run_cost = session.scalar(
        select(func.coalesce(func.sum(UsageEvent.cost_usd), 0)).where(
            UsageEvent.reference_type == "editorial_run",
            UsageEvent.reference_id.in_(run_ids),
        )
    )
    return Decimal(str(project_cost or 0)) + Decimal(str(run_cost or 0))
