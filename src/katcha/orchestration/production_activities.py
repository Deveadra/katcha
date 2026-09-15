from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from temporalio import activity

from katcha.db import session_scope
from katcha.domain import ProductionStatus
from katcha.editorial.generator import generate_short_scripts
from katcha.editorial.personas import get_persona
from katcha.models import DomainEvent, UsageEvent
from katcha.production_models import Production, ProductionScript


class AmbiguousPaidCall(RuntimeError):
    pass


def _production_cost(session: Any, production_id: uuid.UUID) -> Decimal:
    value = session.scalar(
        select(func.coalesce(func.sum(UsageEvent.cost_usd), 0)).where(
            UsageEvent.reference_type == "production",
            UsageEvent.reference_id == str(production_id),
        )
    )
    return Decimal(str(value or 0))


def _active_analysis(snapshot: dict[str, Any]) -> dict[str, Any]:
    ai = snapshot.get("ai_features")
    if not isinstance(ai, dict):
        return {}
    deep = ai.get("deep")
    bulk = ai.get("bulk")
    if isinstance(deep, dict):
        return deep
    if isinstance(bulk, dict):
        return bulk
    return {}


@activity.defn
def generate_script_candidates(production_id: str) -> dict[str, object]:
    production_uuid = uuid.UUID(production_id)
    with session_scope() as session:
        production = session.get(Production, production_uuid)
        if production is None:
            raise ValueError(f"production not found: {production_id}")
        existing = list(
            session.scalars(
                select(ProductionScript)
                .where(ProductionScript.production_id == production_uuid)
                .order_by(ProductionScript.candidate_index)
            )
        )
        if len(existing) == 3:
            return {"production_id": production_id, "candidate_count": 3, "reused": True}
        if production.stage == "script_call_started":
            raise AmbiguousPaidCall(
                "script provider call may already have been accepted; create a new generation "
                "instead of automatically retrying"
            )
        production.status = ProductionStatus.SCRIPTING.value
        production.stage = "script_call_started"
        production.error = None
        persona_key = production.persona_key
        persona_version = production.persona_version
        prompt_version = production.prompt_version
        snapshot = dict(production.analysis_snapshot or {})

    persona = get_persona(persona_key, persona_version)
    result = generate_short_scripts(
        persona,
        snapshot,
        prompt_version=prompt_version,
        production_id=production_id,
    )

    with session_scope() as session:
        production = session.get(Production, production_uuid)
        if production is None:
            raise RuntimeError("production disappeared after script generation")
        existing_count = session.scalar(
            select(func.count(ProductionScript.id)).where(
                ProductionScript.production_id == production_uuid
            )
        )
        if existing_count:
            return {
                "production_id": production_id,
                "candidate_count": int(existing_count),
                "reused": True,
            }

        for index, candidate in enumerate(result.scripts.candidates):
            session.add(
                ProductionScript(
                    production_id=production_uuid,
                    candidate_index=index,
                    style=candidate.style,
                    narration=candidate.narration,
                    interaction_prompt=candidate.interaction_prompt,
                    rationale=candidate.rationale,
                    provider=result.target.provider,
                    model=result.target.model,
                    prompt_version=prompt_version,
                    selected=False,
                    script_metadata={
                        "segments": [
                            segment.model_dump(mode="json") for segment in candidate.segments
                        ],
                        "title_angle": candidate.title_angle,
                    },
                )
            )
        production.status = ProductionStatus.SCRIPTED.value
        production.stage = "scripts_ready"
        production.estimated_cost_usd = _production_cost(session, production_uuid)
        session.add(
            DomainEvent(
                aggregate_type="production",
                aggregate_id=production_id,
                event_type="production.scripts_generated",
                payload={
                    "production_id": production_id,
                    "candidate_count": 3,
                    "provider": result.target.provider,
                    "model": result.target.model,
                    "persona_key": persona_key,
                    "persona_version": persona_version,
                },
            )
        )
    return {"production_id": production_id, "candidate_count": 3, "reused": False}


@activity.defn
def select_script_candidate(production_id: str) -> dict[str, object]:
    production_uuid = uuid.UUID(production_id)
    with session_scope() as session:
        production = session.get(Production, production_uuid)
        if production is None:
            raise ValueError(f"production not found: {production_id}")
        if production.selected_script_id is not None:
            selected = session.get(ProductionScript, production.selected_script_id)
            if selected is not None:
                return {
                    "production_id": production_id,
                    "script_id": str(selected.id),
                    "style": selected.style,
                    "reused": True,
                }

        candidates = list(
            session.scalars(
                select(ProductionScript)
                .where(ProductionScript.production_id == production_uuid)
                .order_by(ProductionScript.candidate_index)
            )
        )
        if len(candidates) != 3:
            raise RuntimeError("exactly three script candidates are required before selection")

        analysis = _active_analysis(dict(production.analysis_snapshot or {}))
        comment_potential = float(analysis.get("comment_potential") or 0)
        humor_score = float(analysis.get("humor_score") or 0)
        if comment_potential >= 75:
            preferred_style = "interactive"
            reason = "high analysis comment-potential signal"
        elif humor_score >= 70:
            preferred_style = "sarcastic"
            reason = "high analysis humor signal"
        else:
            preferred_style = "observational"
            reason = "default low-risk editorial treatment"

        selected = next(
            candidate for candidate in candidates if candidate.style == preferred_style
        )
        for candidate in candidates:
            candidate.selected = candidate.id == selected.id
        production.selected_script_id = selected.id
        production.stage = "script_selected"
        session.add(
            DomainEvent(
                aggregate_type="production",
                aggregate_id=production_id,
                event_type="production.script_selected",
                payload={
                    "production_id": production_id,
                    "script_id": str(selected.id),
                    "style": selected.style,
                    "selection_reason": reason,
                },
            )
        )
        return {
            "production_id": production_id,
            "script_id": str(selected.id),
            "style": selected.style,
            "selection_reason": reason,
            "reused": False,
        }


@activity.defn
def mark_production_failed(production_id: str, message: str) -> None:
    production_uuid = uuid.UUID(production_id)
    with session_scope() as session:
        production = session.get(Production, production_uuid)
        if production is None:
            return
        production.status = ProductionStatus.FAILED.value
        production.stage = "failed"
        production.error = message[:8000]
        production.estimated_cost_usd = _production_cost(session, production_uuid)
        session.add(
            DomainEvent(
                aggregate_type="production",
                aggregate_id=production_id,
                event_type="production.failed",
                payload={"production_id": production_id, "error": production.error},
            )
        )
