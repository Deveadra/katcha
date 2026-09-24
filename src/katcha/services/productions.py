from __future__ import annotations

import hashlib
import uuid
from decimal import Decimal

from sqlalchemy import select

from katcha.db import session_scope
from katcha.domain import ChannelStatus, ProductionStatus, ReviewDecision
from katcha.editorial.personas import get_persona
from katcha.intelligence_models import ChannelProfile
from katcha.models import Clip, ClipFeature, DomainEvent
from katcha.production_models import (
    Production,
    ProductionAsset,
    ProductionReview,
    ProductionScript,
)
from katcha.services.acquisition import (
    ClipAcquisitionState,
    assert_clip_production_eligible,
)
from katcha.services.channel_brands import brand_for_channel
from katcha.services.channel_edit_blueprints import blueprint_for_channel

PROMPT_VERSION = "short-script-v2"
REGENERATE_STAGES = {"script", "voice", "render"}


def _workflow_id(
    clip_id: uuid.UUID,
    idempotency_key: str | None,
    channel_profile_id: uuid.UUID | None,
) -> str:
    scope = str(channel_profile_id) if channel_profile_id else "shared"
    if idempotency_key:
        digest = hashlib.sha256(f"{scope}:{idempotency_key}".encode()).hexdigest()[:20]
        return f"short-prod-{clip_id}-{digest}"
    scope_digest = hashlib.sha256(scope.encode()).hexdigest()[:8]
    return f"short-prod-{clip_id}-{scope_digest}-{uuid.uuid4().hex[:12]}"


def _analysis_snapshot(clip: Clip, features: ClipFeature) -> dict[str, object]:
    return {
        "clip_sha256": clip.sha256,
        "duration_seconds": float(clip.duration_seconds or 0),
        "transcript": features.transcript,
        "local_features": dict(features.local_features or {}),
        "ai_features": dict(features.ai_features or {}),
        "candidate_score": float(features.candidate_score or 0),
        "score_breakdown": dict(features.score_breakdown or {}),
        "features_updated_at": features.updated_at.isoformat() if features.updated_at else None,
    }


def _acquisition_snapshot(state: ClipAcquisitionState) -> dict[str, object]:
    return {
        "managed": state.managed,
        "eligible": state.eligible,
        "candidate_id": str(state.candidate_id) if state.candidate_id else None,
        "assessment_id": str(state.assessment_id) if state.assessment_id else None,
        "rights_lane": state.rights_lane,
        "reason": state.reason,
    }


def _validate_channel_scope(
    session: object,
    channel_profile_id: uuid.UUID | None,
) -> None:
    if channel_profile_id is None:
        return
    profile = session.get(ChannelProfile, channel_profile_id)
    if profile is None:
        raise ValueError(f"channel profile not found: {channel_profile_id}")
    if profile.status != ChannelStatus.ACTIVE.value:
        raise ValueError("channel profile is not active")


def register_short_production(
    clip_id: uuid.UUID,
    *,
    persona_key: str = "youth_host",
    idempotency_key: str | None = None,
    channel_profile_id: uuid.UUID | None = None,
    edit_blueprint_key: str | None = None,
) -> Production:
    workflow_id = _workflow_id(clip_id, idempotency_key, channel_profile_id)

    with session_scope() as session:
        existing = session.scalar(
            select(Production).where(Production.workflow_id == workflow_id)
        )
        if existing is not None:
            if (
                edit_blueprint_key is not None
                and existing.edit_blueprint_key != edit_blueprint_key
            ):
                raise ValueError(
                    "idempotency key is already bound to another edit blueprint"
                )
            session.expunge(existing)
            return existing

        _validate_channel_scope(session, channel_profile_id)
        edit_blueprint, edit_blueprint_version = blueprint_for_channel(
            session,
            channel_profile_id,
            blueprint_key=edit_blueprint_key,
        )
        brand, brand_version = brand_for_channel(session, channel_profile_id)
        if channel_profile_id is None and persona_key != brand.persona.key:
            persona = get_persona(persona_key)
        else:
            persona = get_persona(brand.persona.key, brand.persona.version)

        clip = session.get(Clip, clip_id)
        features = session.get(ClipFeature, clip_id)
        if clip is None:
            raise ValueError(f"clip not found: {clip_id}")
        if features is None or features.candidate_score is None:
            raise ValueError("clip must have completed scoring before production")
        acquisition_state = assert_clip_production_eligible(clip_id)
        snapshot = _analysis_snapshot(clip, features)
        snapshot["acquisition"] = _acquisition_snapshot(acquisition_state)

        production = Production(
            clip_id=clip_id,
            channel_profile_id=channel_profile_id,
            parent_production_id=None,
            generation=1,
            regenerate_from=None,
            workflow_id=workflow_id,
            kind="short",
            status=ProductionStatus.QUEUED.value,
            stage="queued",
            persona_key=persona.key,
            persona_version=persona.version,
            prompt_version=PROMPT_VERSION,
            brand_key=brand.brand_key,
            brand_version=brand_version,
            brand_snapshot=brand.model_dump(mode="json"),
            edit_blueprint_key=edit_blueprint.key,
            edit_blueprint_version=edit_blueprint_version,
            edit_blueprint_snapshot=edit_blueprint.model_dump(mode="json"),
            analysis_snapshot=snapshot,
            estimated_cost_usd=Decimal("0"),
        )
        session.add(production)
        session.flush()
        session.add(
            DomainEvent(
                aggregate_type="production",
                aggregate_id=str(production.id),
                event_type="production.created",
                payload={
                    "production_id": str(production.id),
                    "clip_id": str(clip_id),
                    "channel_profile_id": (
                        str(channel_profile_id) if channel_profile_id else None
                    ),
                    "brand_key": production.brand_key,
                    "brand_version": production.brand_version,
                    "edit_blueprint_key": production.edit_blueprint_key,
                    "edit_blueprint_version": production.edit_blueprint_version,
                    "generation": 1,
                    "acquisition_managed": acquisition_state.managed,
                    "rights_lane": acquisition_state.rights_lane,
                },
            )
        )
        session.refresh(production)
        session.expunge(production)
        return production


def _clone_scripts(session: object, parent: Production, child: Production) -> None:
    scripts = list(
        session.scalars(
            select(ProductionScript)
            .where(ProductionScript.production_id == parent.id)
            .order_by(ProductionScript.candidate_index)
        )
    )
    if len(scripts) != 3:
        raise ValueError("parent production does not have a complete script set")

    selected_child: ProductionScript | None = None
    for source in scripts:
        clone = ProductionScript(
            production_id=child.id,
            candidate_index=source.candidate_index,
            style=source.style,
            narration=source.narration,
            interaction_prompt=source.interaction_prompt,
            rationale=source.rationale,
            provider=source.provider,
            model=source.model,
            prompt_version=source.prompt_version,
            selected=source.selected,
            script_metadata=dict(source.script_metadata or {}),
        )
        session.add(clone)
        if source.selected:
            selected_child = clone
    session.flush()
    if selected_child is None:
        raise ValueError("parent production has no selected script")
    child.selected_script_id = selected_child.id


def _clone_voice_assets(session: object, parent: Production, child: Production) -> None:
    assets = list(
        session.scalars(
            select(ProductionAsset).where(ProductionAsset.production_id == parent.id)
        )
    )
    reusable = [asset for asset in assets if asset.kind.startswith("narration_")]
    if not reusable:
        blueprint = dict(parent.edit_blueprint_snapshot or {})
        narration = dict(blueprint.get("narration") or {})
        if narration.get("mode") in {"text_only", "source_only"}:
            child.selected_voice_profile = parent.selected_voice_profile
            return
        raise ValueError("parent production has no narration assets to reuse")
    for source in reusable:
        session.add(
            ProductionAsset(
                production_id=child.id,
                kind=source.kind,
                generation=1,
                storage_key=source.storage_key,
                content_type=source.content_type,
                provider=source.provider,
                model=source.model,
                asset_metadata={
                    **dict(source.asset_metadata or {}),
                    "reused_from": str(parent.id),
                },
            )
        )
    child.selected_voice_profile = parent.selected_voice_profile


def register_regeneration(
    production_id: uuid.UUID,
    *,
    stage: str,
    note: str | None = None,
    actor: str = "operator",
) -> Production:
    if stage not in REGENERATE_STAGES:
        raise ValueError(f"regenerate stage must be one of: {sorted(REGENERATE_STAGES)}")

    with session_scope() as session:
        parent = session.get(Production, production_id)
        if parent is None:
            raise ValueError(f"production not found: {production_id}")
        if parent.status not in {
            ProductionStatus.REVIEW.value,
            ProductionStatus.REJECTED.value,
            ProductionStatus.FAILED.value,
        }:
            raise ValueError(
                "production must be in review, rejected, or failed state to regenerate"
            )
        _validate_channel_scope(session, parent.channel_profile_id)
        acquisition_state = assert_clip_production_eligible(parent.clip_id)
        child_snapshot = dict(parent.analysis_snapshot or {})
        child_snapshot["acquisition"] = _acquisition_snapshot(acquisition_state)

        child = Production(
            clip_id=parent.clip_id,
            channel_profile_id=parent.channel_profile_id,
            parent_production_id=parent.id,
            generation=parent.generation + 1,
            regenerate_from=stage,
            workflow_id=_workflow_id(parent.clip_id, None, parent.channel_profile_id),
            kind=parent.kind,
            status=ProductionStatus.QUEUED.value,
            stage=f"regenerate_{stage}_queued",
            persona_key=parent.persona_key,
            persona_version=parent.persona_version,
            prompt_version=parent.prompt_version,
            brand_key=parent.brand_key,
            brand_version=parent.brand_version,
            brand_snapshot=(
                dict(parent.brand_snapshot) if parent.brand_snapshot is not None else None
            ),
            edit_blueprint_key=parent.edit_blueprint_key,
            edit_blueprint_version=parent.edit_blueprint_version,
            edit_blueprint_snapshot=(
                dict(parent.edit_blueprint_snapshot)
                if parent.edit_blueprint_snapshot is not None
                else None
            ),
            analysis_snapshot=child_snapshot,
            estimated_cost_usd=Decimal("0"),
        )
        session.add(child)
        session.flush()

        if stage in {"voice", "render"}:
            _clone_scripts(session, parent, child)
            child.status = ProductionStatus.SCRIPTED.value
            child.stage = "script_selected"
        if stage == "render":
            _clone_voice_assets(session, parent, child)
            child.status = ProductionStatus.VOICED.value
            child.stage = "voice_ready"

        parent.status = ProductionStatus.REJECTED.value
        parent.stage = f"regenerated_from_{stage}"
        session.add(
            ProductionReview(
                production_id=parent.id,
                decision=ReviewDecision.REGENERATE.value,
                actor=actor,
                note=note,
                review_metadata={
                    "regenerate_from": stage,
                    "child_production_id": str(child.id),
                },
            )
        )
        session.add(
            DomainEvent(
                aggregate_type="production",
                aggregate_id=str(parent.id),
                event_type="production.regenerated",
                payload={
                    "production_id": str(parent.id),
                    "child_production_id": str(child.id),
                    "channel_profile_id": (
                        str(parent.channel_profile_id) if parent.channel_profile_id else None
                    ),
                    "brand_key": child.brand_key,
                    "brand_version": child.brand_version,
                    "edit_blueprint_key": child.edit_blueprint_key,
                    "edit_blueprint_version": child.edit_blueprint_version,
                    "regenerate_from": stage,
                    "acquisition_managed": acquisition_state.managed,
                    "rights_lane": acquisition_state.rights_lane,
                },
            )
        )
        session.refresh(child)
        session.expunge(child)
        return child


def review_production(
    production_id: uuid.UUID,
    *,
    decision: ReviewDecision,
    note: str | None = None,
    actor: str = "operator",
) -> ProductionReview:
    if decision == ReviewDecision.REGENERATE:
        raise ValueError("use register_regeneration for regenerate decisions")

    with session_scope() as session:
        production = session.get(Production, production_id)
        if production is None:
            raise ValueError(f"production not found: {production_id}")
        if production.status != ProductionStatus.REVIEW.value:
            raise ValueError("production is not awaiting review")

        review = ProductionReview(
            production_id=production.id,
            decision=decision.value,
            actor=actor,
            note=note,
            review_metadata={},
        )
        session.add(review)
        if decision == ReviewDecision.APPROVE:
            production.status = ProductionStatus.APPROVED.value
            production.stage = "approved"
            event_type = "production.approved"
        else:
            production.status = ProductionStatus.REJECTED.value
            production.stage = "rejected"
            event_type = "production.rejected"
        session.add(
            DomainEvent(
                aggregate_type="production",
                aggregate_id=str(production.id),
                event_type=event_type,
                payload={
                    "production_id": str(production.id),
                    "channel_profile_id": (
                        str(production.channel_profile_id)
                        if production.channel_profile_id
                        else None
                    ),
                    "decision": decision.value,
                    "actor": actor,
                },
            )
        )
        session.flush()
        session.refresh(review)
        session.expunge(review)
        return review
