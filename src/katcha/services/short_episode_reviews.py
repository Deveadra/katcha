from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select

from katcha.db import session_scope
from katcha.domain import ReviewDecision
from katcha.models import DomainEvent
from katcha.services.acquisition import ClipAcquisitionState, assert_clip_production_eligible
from katcha.services.render_qc import assert_render_qc_passed
from katcha.short_episode_models import (
    ShortEpisode,
    ShortEpisodeAsset,
    ShortEpisodeItem,
    ShortEpisodeReview,
    ShortEpisodeScript,
)

REGENERATE_STAGES = {"script", "voice", "render"}


def _regeneration_workflow_id(channel_profile_id: uuid.UUID) -> str:
    return f"short-episode-{channel_profile_id.hex[:8]}-{uuid.uuid4().hex[:16]}"


def _acquisition_snapshot(state: ClipAcquisitionState) -> dict[str, object]:
    return {
        "managed": state.managed,
        "eligible": state.eligible,
        "candidate_id": str(state.candidate_id) if state.candidate_id else None,
        "assessment_id": str(state.assessment_id) if state.assessment_id else None,
        "rights_lane": state.rights_lane,
        "reason": state.reason,
    }


def _clone_items(session: object, parent: ShortEpisode, child: ShortEpisode) -> None:
    items = list(
        session.scalars(
            select(ShortEpisodeItem)
            .where(ShortEpisodeItem.short_episode_id == parent.id)
            .order_by(ShortEpisodeItem.position.desc())
        )
    )
    if len(items) != parent.item_count:
        raise ValueError("parent short episode item plan is incomplete")

    for source in items:
        acquisition = assert_clip_production_eligible(source.clip_id)
        session.add(
            ShortEpisodeItem(
                short_episode_id=child.id,
                position=source.position,
                clip_id=source.clip_id,
                role=source.role,
                overall_score=source.overall_score,
                role_score=source.role_score,
                editorial_signals=dict(source.editorial_signals or {}),
                analysis_snapshot=dict(source.analysis_snapshot or {}),
                acquisition_snapshot=_acquisition_snapshot(acquisition),
                source_snapshot=list(source.source_snapshot or []),
            )
        )


def _clone_scripts(session: object, parent: ShortEpisode, child: ShortEpisode) -> None:
    scripts = list(
        session.scalars(
            select(ShortEpisodeScript)
            .where(ShortEpisodeScript.short_episode_id == parent.id)
            .order_by(ShortEpisodeScript.candidate_index)
        )
    )
    if len(scripts) != 3:
        raise ValueError("parent short episode does not have a complete script set")

    selected_child: ShortEpisodeScript | None = None
    for source in scripts:
        clone = ShortEpisodeScript(
            short_episode_id=child.id,
            candidate_index=source.candidate_index,
            style=source.style,
            script_payload=dict(source.script_payload or {}),
            narration_beats=list(source.narration_beats or []),
            provider=source.provider,
            model=source.model,
            prompt_version=source.prompt_version,
            selected=source.selected,
        )
        session.add(clone)
        if source.selected:
            selected_child = clone
    session.flush()
    if selected_child is None:
        raise ValueError("parent short episode has no selected script")
    child.selected_script_id = selected_child.id


def _clone_narration_assets(session: object, parent: ShortEpisode, child: ShortEpisode) -> None:
    assets = list(
        session.scalars(
            select(ShortEpisodeAsset)
            .where(
                ShortEpisodeAsset.short_episode_id == parent.id,
                ShortEpisodeAsset.kind.like("narration_%"),
                ShortEpisodeAsset.generation == 1,
            )
            .order_by(ShortEpisodeAsset.kind)
        )
    )
    if not assets:
        raise ValueError("render regeneration requires parent narration assets")
    for source in assets:
        session.add(
            ShortEpisodeAsset(
                short_episode_id=child.id,
                kind=source.kind,
                generation=1,
                storage_key=source.storage_key,
                content_type=source.content_type,
                provider=source.provider,
                model=source.model,
                asset_metadata=dict(source.asset_metadata or {}),
            )
        )
    child.selected_voice_profile = parent.selected_voice_profile


def register_short_episode_regeneration(
    episode_id: uuid.UUID,
    *,
    stage: str,
    note: str | None = None,
    actor: str = "operator",
) -> ShortEpisode:
    if stage not in REGENERATE_STAGES:
        raise ValueError(f"regenerate stage must be one of: {sorted(REGENERATE_STAGES)}")

    with session_scope() as session:
        parent = session.get(ShortEpisode, episode_id)
        if parent is None:
            raise ValueError(f"short episode not found: {episode_id}")
        if parent.status not in {
            "voiced",
            "review",
            "editorial_approved",
            "rendering",
            "rendered",
            "rejected",
            "failed",
            "approved",
        }:
            raise ValueError("short episode is not in a regenerable state")
        if stage in {"voice", "render"} and parent.selected_script_id is None:
            raise ValueError(f"{stage} regeneration requires a selected parent script")

        child = ShortEpisode(
            channel_profile_id=parent.channel_profile_id,
            trend_opportunity_id=parent.trend_opportunity_id,
            parent_episode_id=parent.id,
            generation=parent.generation + 1,
            regenerate_from=stage,
            workflow_id=_regeneration_workflow_id(parent.channel_profile_id),
            status="planned",
            stage=f"regenerate_{stage}_queued",
            premise=parent.premise,
            format_key=parent.format_key,
            format_version=parent.format_version,
            item_count=parent.item_count,
            format_snapshot=dict(parent.format_snapshot or {}),
            plan_snapshot=dict(parent.plan_snapshot or {}),
            persona_key=parent.persona_key,
            persona_version=parent.persona_version,
            prompt_version=parent.prompt_version,
            brand_key=parent.brand_key,
            brand_version=parent.brand_version,
            brand_snapshot=dict(parent.brand_snapshot or {}),
            edit_blueprint_key=parent.edit_blueprint_key,
            edit_blueprint_version=parent.edit_blueprint_version,
            edit_blueprint_snapshot=(
                dict(parent.edit_blueprint_snapshot)
                if parent.edit_blueprint_snapshot is not None
                else None
            ),
            render_manifest={},
            estimated_cost_usd=Decimal("0"),
        )
        session.add(child)
        session.flush()
        _clone_items(session, parent, child)

        if stage in {"voice", "render"}:
            _clone_scripts(session, parent, child)
            child.status = "scripted"
            child.stage = "script_selected"
        if stage == "render":
            _clone_narration_assets(session, parent, child)
            child.status = "editorial_approved"
            child.stage = "render_regeneration_queued"

        parent.status = "rejected"
        parent.stage = f"regenerated_from_{stage}"
        session.add(
            ShortEpisodeReview(
                short_episode_id=parent.id,
                decision=ReviewDecision.REGENERATE.value,
                actor=actor,
                note=note,
                review_metadata={
                    "regenerate_from": stage,
                    "child_episode_id": str(child.id),
                    "review_phase": (
                        "render" if stage == "render" else "editorial"
                    ),
                },
            )
        )
        session.add(
            DomainEvent(
                aggregate_type="short_episode",
                aggregate_id=str(parent.id),
                event_type="short_episode.regenerated",
                payload={
                    "short_episode_id": str(parent.id),
                    "child_episode_id": str(child.id),
                    "channel_profile_id": str(parent.channel_profile_id),
                    "brand_key": child.brand_key,
                    "brand_version": child.brand_version,
                    "edit_blueprint_key": child.edit_blueprint_key,
                    "edit_blueprint_version": child.edit_blueprint_version,
                    "trend_opportunity_id": (
                        str(child.trend_opportunity_id) if child.trend_opportunity_id else None
                    ),
                    "regenerate_from": stage,
                },
            )
        )
        session.flush()
        session.refresh(child)
        session.expunge(child)
        return child


def review_short_episode(
    episode_id: uuid.UUID,
    *,
    decision: ReviewDecision,
    note: str | None = None,
    actor: str = "operator",
) -> ShortEpisodeReview:
    if decision == ReviewDecision.REGENERATE:
        raise ValueError("use register_short_episode_regeneration for regenerate decisions")

    with session_scope() as session:
        episode = session.get(ShortEpisode, episode_id)
        if episode is None:
            raise ValueError(f"short episode not found: {episode_id}")
        if episode.status in {"voiced", "review"}:
            review_phase = "editorial"
        elif episode.status in {"rendered", "render_review"}:
            review_phase = "render"
        else:
            raise ValueError("short episode is not awaiting review")

        review = ShortEpisodeReview(
            short_episode_id=episode.id,
            decision=decision.value,
            actor=actor,
            note=note,
            review_metadata={"review_phase": review_phase},
        )
        session.add(review)
        if decision == ReviewDecision.APPROVE and review_phase == "editorial":
            episode.status = "editorial_approved"
            episode.stage = "editorial_approved"
            event_type = "short_episode.editorial_approved"
        elif decision == ReviewDecision.APPROVE:
            render_asset = session.scalar(
                select(ShortEpisodeAsset).where(
                    ShortEpisodeAsset.short_episode_id == episode.id,
                    ShortEpisodeAsset.kind == "render",
                    ShortEpisodeAsset.generation == 1,
                )
            )
            if render_asset is None:
                raise ValueError("short episode cannot be approved without a render asset")
            assert_render_qc_passed(render_asset.asset_metadata)
            episode.status = "approved"
            episode.stage = "render_approved"
            event_type = "short_episode.approved"
        else:
            episode.status = "rejected"
            episode.stage = f"{review_phase}_rejected"
            event_type = "short_episode.rejected"
        session.add(
            DomainEvent(
                aggregate_type="short_episode",
                aggregate_id=str(episode.id),
                event_type=event_type,
                payload={
                    "short_episode_id": str(episode.id),
                    "channel_profile_id": str(episode.channel_profile_id),
                    "decision": decision.value,
                    "review_phase": review_phase,
                    "actor": actor,
                },
            )
        )
        session.flush()
        session.refresh(review)
        session.expunge(review)
        return review
