from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select

from katcha.db import session_scope
from katcha.editorial.personas import get_persona
from katcha.editorial.rankings import (
    RankingCandidateSignals,
    RankingEpisodePlan,
    RankingFormatContract,
    build_ranking_episode_plan,
    get_ranking_format,
)
from katcha.models import Clip, ClipFeature, DomainEvent, SourceItem
from katcha.services.acquisition import ClipAcquisitionState, assert_clip_production_eligible
from katcha.services.channel_brands import brand_for_channel
from katcha.services.channel_profiles import ensure_active_profile
from katcha.short_episode_models import ShortEpisode, ShortEpisodeItem


@dataclass(frozen=True, slots=True)
class ShortEpisodeCandidateInput:
    clip_id: uuid.UUID
    hook_strength: float
    visual_clarity: float
    payoff_strength: float
    escalation_value: float
    commentary_opportunity: float
    novelty: float
    source_quality: float

    def ranking_signals(self) -> RankingCandidateSignals:
        return RankingCandidateSignals(
            candidate_id=str(self.clip_id),
            hook_strength=self.hook_strength,
            visual_clarity=self.visual_clarity,
            payoff_strength=self.payoff_strength,
            escalation_value=self.escalation_value,
            commentary_opportunity=self.commentary_opportunity,
            novelty=self.novelty,
            source_quality=self.source_quality,
            rights_ready=True,
        )

    def snapshot(self) -> dict[str, float]:
        return {
            "hook_strength": self.hook_strength,
            "visual_clarity": self.visual_clarity,
            "payoff_strength": self.payoff_strength,
            "escalation_value": self.escalation_value,
            "commentary_opportunity": self.commentary_opportunity,
            "novelty": self.novelty,
            "source_quality": self.source_quality,
        }


def _workflow_id(channel_profile_id: uuid.UUID, idempotency_key: str | None) -> str:
    if idempotency_key and idempotency_key.strip():
        digest = hashlib.sha256(
            f"{channel_profile_id}:{idempotency_key.strip()}".encode()
        ).hexdigest()[:24]
        return f"short-episode-{digest}"
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


def _analysis_snapshot(clip: Clip, features: ClipFeature) -> dict[str, object]:
    return {
        "clip_sha256": clip.sha256,
        "duration_seconds": float(clip.duration_seconds or 0),
        "candidate_score": float(features.candidate_score or 0),
        "score_breakdown": dict(features.score_breakdown or {}),
        "local_features": dict(features.local_features or {}),
        "ai_features": dict(features.ai_features or {}),
        "transcript": features.transcript,
        "transcript_language": features.transcript_language,
        "features_updated_at": (
            features.updated_at.isoformat() if features.updated_at is not None else None
        ),
    }


def _source_snapshot(sources: list[SourceItem]) -> list[dict[str, object]]:
    return [
        {
            "source_item_id": str(source.id),
            "source_url": source.source_url,
            "canonical_url": source.canonical_url,
            "platform": source.platform,
            "title": source.title,
            "creator": source.creator,
            "source_metadata": dict(source.source_metadata or {}),
        }
        for source in sources
    ]


def _resolve_format(
    brand_format_key: str | None,
    brand_format_version: str | None,
    requested_key: str | None,
    requested_version: str | None,
) -> RankingFormatContract:
    if not brand_format_key or not brand_format_version:
        raise ValueError("channel brand does not define a ranked short editorial format")
    key = requested_key or brand_format_key
    version = requested_version or brand_format_version
    if key != brand_format_key or version != brand_format_version:
        raise ValueError(
            "requested editorial format is not enabled by the active channel brand"
        )
    try:
        return get_ranking_format(key, version)
    except KeyError as exc:
        raise ValueError(str(exc)) from exc


def _validate_candidate_ids(candidates: list[ShortEpisodeCandidateInput]) -> None:
    if not candidates:
        raise ValueError("short episode requires candidate clips")
    ids = [candidate.clip_id for candidate in candidates]
    if len(ids) != len(set(ids)):
        raise ValueError("short episode candidate clips must be unique")


def _persist_episode_items(
    session: object,
    episode: ShortEpisode,
    plan: RankingEpisodePlan,
    candidate_by_id: dict[str, ShortEpisodeCandidateInput],
    analysis_by_id: dict[str, dict[str, object]],
    acquisition_by_id: dict[str, dict[str, object]],
    sources_by_id: dict[str, list[dict[str, object]]],
) -> None:
    for planned in plan.ordered_items:
        candidate = candidate_by_id[planned.candidate_id]
        session.add(
            ShortEpisodeItem(
                short_episode_id=episode.id,
                position=planned.position,
                clip_id=candidate.clip_id,
                role=planned.role,
                overall_score=Decimal(str(planned.overall_score)),
                role_score=Decimal(str(planned.role_score)),
                editorial_signals=candidate.snapshot(),
                analysis_snapshot=analysis_by_id[planned.candidate_id],
                acquisition_snapshot=acquisition_by_id[planned.candidate_id],
                source_snapshot=sources_by_id[planned.candidate_id],
            )
        )


def register_short_episode(
    *,
    channel_profile_id: uuid.UUID,
    premise: str,
    candidates: list[ShortEpisodeCandidateInput],
    item_count: int | None = None,
    format_key: str | None = None,
    format_version: str | None = None,
    idempotency_key: str | None = None,
) -> ShortEpisode:
    """Plan and persist a short multi-clip episode before any paid editorial call."""
    normalized_premise = premise.strip()
    if not normalized_premise:
        raise ValueError("short episode premise cannot be empty")
    if len(normalized_premise) > 500:
        raise ValueError("short episode premise cannot exceed 500 characters")
    _validate_candidate_ids(candidates)

    workflow_id = _workflow_id(channel_profile_id, idempotency_key)
    with session_scope() as session:
        existing = session.scalar(
            select(ShortEpisode).where(ShortEpisode.workflow_id == workflow_id)
        )
        if existing is not None:
            session.expunge(existing)
            return existing

        profile = ensure_active_profile(session, channel_profile_id)
        brand, brand_version = brand_for_channel(session, profile.id)
        brand_format = brand.editorial_format
        contract = _resolve_format(
            brand_format.key if brand_format else None,
            brand_format.version if brand_format else None,
            format_key,
            format_version,
        )
        persona = get_persona(brand.persona.key, brand.persona.version)

        candidate_by_id: dict[str, ShortEpisodeCandidateInput] = {}
        analysis_by_id: dict[str, dict[str, object]] = {}
        acquisition_by_id: dict[str, dict[str, object]] = {}
        sources_by_id: dict[str, list[dict[str, object]]] = {}
        ranking_candidates: list[RankingCandidateSignals] = []

        for candidate in candidates:
            clip = session.get(Clip, candidate.clip_id)
            if clip is None:
                raise ValueError(f"clip not found: {candidate.clip_id}")
            features = session.get(ClipFeature, candidate.clip_id)
            if features is None or features.candidate_score is None:
                raise ValueError(
                    f"clip must have completed analysis/scoring: {candidate.clip_id}"
                )

            acquisition = assert_clip_production_eligible(candidate.clip_id)
            if not acquisition.eligible:
                raise ValueError(f"clip is not production eligible: {candidate.clip_id}")

            sources = list(
                session.scalars(
                    select(SourceItem)
                    .where(SourceItem.clip_id == candidate.clip_id)
                    .order_by(SourceItem.discovered_at.asc())
                )
            )
            candidate_id = str(candidate.clip_id)
            candidate_by_id[candidate_id] = candidate
            analysis_by_id[candidate_id] = _analysis_snapshot(clip, features)
            acquisition_by_id[candidate_id] = _acquisition_snapshot(acquisition)
            sources_by_id[candidate_id] = _source_snapshot(sources)
            ranking_candidates.append(candidate.ranking_signals())

        plan = build_ranking_episode_plan(
            ranking_candidates,
            premise=normalized_premise,
            item_count=item_count,
            contract=contract,
        )
        episode = ShortEpisode(
            channel_profile_id=profile.id,
            parent_episode_id=None,
            generation=1,
            regenerate_from=None,
            workflow_id=workflow_id,
            status="planned",
            stage="planned",
            premise=normalized_premise,
            format_key=contract.key,
            format_version=contract.version,
            item_count=plan.item_count,
            format_snapshot=contract.model_dump(mode="json"),
            plan_snapshot=plan.model_dump(mode="json"),
            persona_key=persona.key,
            persona_version=persona.version,
            brand_key=brand.brand_key,
            brand_version=brand_version,
            brand_snapshot=brand.model_dump(mode="json"),
            estimated_cost_usd=Decimal("0"),
        )
        session.add(episode)
        session.flush()
        _persist_episode_items(
            session,
            episode,
            plan,
            candidate_by_id,
            analysis_by_id,
            acquisition_by_id,
            sources_by_id,
        )
        session.add(
            DomainEvent(
                aggregate_type="short_episode",
                aggregate_id=str(episode.id),
                event_type="short_episode.planned",
                payload={
                    "short_episode_id": str(episode.id),
                    "channel_profile_id": str(profile.id),
                    "workflow_id": workflow_id,
                    "brand_key": brand.brand_key,
                    "brand_version": brand_version,
                    "format_key": contract.key,
                    "format_version": contract.version,
                    "item_count": plan.item_count,
                    "ordered_clip_ids": [item.candidate_id for item in plan.ordered_items],
                },
            )
        )
        session.flush()
        session.refresh(episode)
        session.expunge(episode)
        return episode


def short_episode_items(short_episode_id: uuid.UUID) -> list[ShortEpisodeItem]:
    with session_scope() as session:
        episode = session.get(ShortEpisode, short_episode_id)
        if episode is None:
            raise ValueError(f"short episode not found: {short_episode_id}")
        items = list(
            session.scalars(
                select(ShortEpisodeItem)
                .where(ShortEpisodeItem.short_episode_id == short_episode_id)
                .order_by(ShortEpisodeItem.position.desc())
            )
        )
        for item in items:
            session.expunge(item)
        return items
