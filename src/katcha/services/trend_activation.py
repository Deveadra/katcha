from __future__ import annotations

import hashlib
import uuid
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import select

from katcha.acquisition_models import DiscoveryCandidate
from katcha.db import session_scope
from katcha.editorial.rankings import (
    RankingCandidateSignals,
    build_ranking_episode_plan,
    get_ranking_format,
)
from katcha.models import ClipFeature, DomainEvent, SourceItem
from katcha.services.acquisition import latest_rights_assessment
from katcha.services.channel_brands import brand_for_channel
from katcha.services.channel_profiles import ensure_active_profile
from katcha.services.short_episodes import (
    ShortEpisodeCandidateInput,
    register_short_episode,
)
from katcha.services.trend_editorial_context import freeze_trend_context
from katcha.short_episode_models import ShortEpisode
from katcha.trend_models import TrendEvidencePacket, TrendOpportunity

_ACTIVATION_ALGORITHM = "trend-opportunity-activation-v1"


@dataclass(frozen=True, slots=True)
class ActivationCandidate:
    discovery_candidate_id: uuid.UUID
    source_item_id: uuid.UUID
    clip_id: uuid.UUID
    candidate_score: float
    signals: ShortEpisodeCandidateInput
    derivation: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ActivationExclusion:
    reference: str
    reason: str
    discovery_candidate_id: uuid.UUID | None = None
    source_item_id: uuid.UUID | None = None
    clip_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class ActivationPreview:
    channel_profile_id: uuid.UUID
    trend_opportunity_id: uuid.UUID
    evidence_packet_id: uuid.UUID
    evidence_packet_sha256: str
    premise: str
    format_key: str
    format_version: str
    item_count: int
    activation_key: str
    ready: bool
    readiness_reason: str
    eligible: tuple[ActivationCandidate, ...]
    excluded: tuple[ActivationExclusion, ...]
    ordered_clip_ids: tuple[uuid.UUID, ...]

    def snapshot(self) -> dict[str, Any]:
        return {
            "algorithm": _ACTIVATION_ALGORITHM,
            "activation_key": self.activation_key,
            "trend_opportunity_id": str(self.trend_opportunity_id),
            "evidence_packet_id": str(self.evidence_packet_id),
            "evidence_packet_sha256": self.evidence_packet_sha256,
            "format_key": self.format_key,
            "format_version": self.format_version,
            "item_count": self.item_count,
            "eligible": [
                {
                    "discovery_candidate_id": str(item.discovery_candidate_id),
                    "source_item_id": str(item.source_item_id),
                    "clip_id": str(item.clip_id),
                    "candidate_score": item.candidate_score,
                    "signals": item.signals.snapshot(),
                    "derivation": item.derivation,
                }
                for item in self.eligible
            ],
            "excluded": [
                {
                    **asdict(item),
                    "discovery_candidate_id": (
                        str(item.discovery_candidate_id)
                        if item.discovery_candidate_id
                        else None
                    ),
                    "source_item_id": str(item.source_item_id) if item.source_item_id else None,
                    "clip_id": str(item.clip_id) if item.clip_id else None,
                }
                for item in self.excluded
            ],
            "ordered_clip_ids": [str(value) for value in self.ordered_clip_ids],
        }


def _clamp(value: object, fallback: float) -> float:
    try:
        return max(0.0, min(100.0, float(value)))
    except (TypeError, ValueError):
        return max(0.0, min(100.0, fallback))


def _optional_number(values: dict[str, Any], key: str) -> float | None:
    value = values.get(key)
    if value is None:
        return None
    try:
        return max(0.0, min(100.0, float(value)))
    except (TypeError, ValueError):
        return None


def derive_episode_signals(
    clip_id: uuid.UUID,
    features: ClipFeature,
) -> tuple[ShortEpisodeCandidateInput, dict[str, Any]]:
    """Map existing feature evidence to the ranked-episode contract without AI calls."""
    base = _clamp(features.candidate_score, 50.0)
    breakdown = dict(features.score_breakdown or {})

    hook = _optional_number(breakdown, "ai_hook")
    surprise = _optional_number(breakdown, "ai_surprise")
    humor = _optional_number(breakdown, "ai_humor")
    comments = _optional_number(breakdown, "ai_comments")
    rewatch = _optional_number(breakdown, "ai_rewatch")
    visual_change = _optional_number(breakdown, "visual_change")
    duration_fit = _optional_number(breakdown, "duration_fit")
    source_reach = _optional_number(breakdown, "source_reach")
    source_engagement = _optional_number(breakdown, "source_engagement")

    hook_strength = hook if hook is not None else base
    visual_values = [
        value for value in (visual_change, duration_fit) if value is not None
    ]
    visual_clarity = sum(visual_values) / len(visual_values) if visual_values else base
    payoff_strength = max(
        [base, *[value for value in (surprise, rewatch) if value is not None]]
    )
    escalation_values = [
        value for value in (surprise, rewatch, visual_change) if value is not None
    ]
    escalation_value = (
        sum(escalation_values) / len(escalation_values)
        if escalation_values
        else base
    )
    commentary_values = [
        value
        for value in (comments, humor, source_engagement)
        if value is not None
    ]
    commentary_opportunity = (
        max(commentary_values) if commentary_values else base
    )
    novelty_values = [
        value for value in (surprise, visual_change) if value is not None
    ]
    novelty = max(novelty_values) if novelty_values else base
    source_values = [
        value for value in (source_reach, source_engagement) if value is not None
    ]
    source_quality = sum(source_values) / len(source_values) if source_values else base

    signal = ShortEpisodeCandidateInput(
        clip_id=clip_id,
        hook_strength=round(hook_strength, 4),
        visual_clarity=round(visual_clarity, 4),
        payoff_strength=round(payoff_strength, 4),
        escalation_value=round(escalation_value, 4),
        commentary_opportunity=round(commentary_opportunity, 4),
        novelty=round(novelty, 4),
        source_quality=round(source_quality, 4),
    )
    return signal, {
        "algorithm": _ACTIVATION_ALGORITHM,
        "candidate_score": base,
        "score_breakdown_keys": sorted(breakdown),
        "fallback": "candidate_score",
    }


def _candidate_references(packet: TrendEvidencePacket) -> tuple[list[str], list[ActivationExclusion]]:
    raw_refs: list[str] = []
    exclusions: list[ActivationExclusion] = []
    seen: set[str] = set()
    for media in packet.media_refs or []:
        if not isinstance(media, dict):
            continue
        raw = media.get("discovery_candidate_id")
        if raw is None:
            continue
        reference = str(raw).strip()
        if not reference or reference in seen:
            continue
        seen.add(reference)
        try:
            uuid.UUID(reference)
        except ValueError:
            exclusions.append(
                ActivationExclusion(
                    reference=reference,
                    reason="invalid_discovery_candidate_id",
                )
            )
            continue
        raw_refs.append(reference)
    return raw_refs, exclusions


def _activation_identity(
    channel_profile_id: uuid.UUID,
    opportunity_id: uuid.UUID,
    packet_sha256: str,
    format_key: str,
    format_version: str,
    item_count: int,
) -> str:
    material = (
        f"{channel_profile_id}:{opportunity_id}:{packet_sha256}:"
        f"{format_key}:{format_version}:{item_count}"
    )
    return hashlib.sha256(material.encode()).hexdigest()[:32]


def preview_trend_activation(
    channel_profile_id: uuid.UUID,
    opportunity_id: uuid.UUID,
    *,
    item_count: int | None = None,
) -> ActivationPreview:
    with session_scope() as session:
        profile = ensure_active_profile(session, channel_profile_id)
        context = freeze_trend_context(session, profile.id, opportunity_id)
        opportunity = session.get(TrendOpportunity, opportunity_id)
        packet = session.get(TrendEvidencePacket, uuid.UUID(context["packet_id"]))
        if opportunity is None or packet is None:
            raise ValueError("trend opportunity evidence disappeared during activation")

        brand, _ = brand_for_channel(session, profile.id)
        brand_format = brand.editorial_format
        if brand_format is None:
            raise ValueError("channel brand does not define a ranked short editorial format")
        contract = get_ranking_format(brand_format.key, brand_format.version)
        count = item_count or brand_format.default_item_count
        if count not in contract.allowed_item_counts:
            raise ValueError(
                f"item_count must be one of {list(contract.allowed_item_counts)} "
                f"for {contract.key}"
            )
        premise = (packet.thesis or opportunity.lifecycle).strip()[:500]
        references, exclusions = _candidate_references(packet)

        eligible: list[ActivationCandidate] = []
        seen_clips: set[uuid.UUID] = set()
        for reference in references:
            candidate_id = uuid.UUID(reference)
            candidate = session.get(DiscoveryCandidate, candidate_id)
            if candidate is None:
                exclusions.append(
                    ActivationExclusion(
                        reference=reference,
                        discovery_candidate_id=candidate_id,
                        reason="discovery_candidate_missing",
                    )
                )
                continue
            if candidate.source_item_id is None:
                exclusions.append(
                    ActivationExclusion(
                        reference=reference,
                        discovery_candidate_id=candidate.id,
                        reason="candidate_not_promoted",
                    )
                )
                continue
            source = session.get(SourceItem, candidate.source_item_id)
            if source is None:
                exclusions.append(
                    ActivationExclusion(
                        reference=reference,
                        discovery_candidate_id=candidate.id,
                        source_item_id=candidate.source_item_id,
                        reason="source_item_missing",
                    )
                )
                continue
            if source.clip_id is None:
                exclusions.append(
                    ActivationExclusion(
                        reference=reference,
                        discovery_candidate_id=candidate.id,
                        source_item_id=source.id,
                        reason="source_not_ingested",
                    )
                )
                continue
            if source.clip_id in seen_clips:
                exclusions.append(
                    ActivationExclusion(
                        reference=reference,
                        discovery_candidate_id=candidate.id,
                        source_item_id=source.id,
                        clip_id=source.clip_id,
                        reason="duplicate_clip",
                    )
                )
                continue
            features = session.get(ClipFeature, source.clip_id)
            if features is None or features.candidate_score is None:
                exclusions.append(
                    ActivationExclusion(
                        reference=reference,
                        discovery_candidate_id=candidate.id,
                        source_item_id=source.id,
                        clip_id=source.clip_id,
                        reason="clip_not_scored",
                    )
                )
                continue
            assessment = latest_rights_assessment(session, candidate.id)
            if assessment is None or not assessment.production_eligible:
                exclusions.append(
                    ActivationExclusion(
                        reference=reference,
                        discovery_candidate_id=candidate.id,
                        source_item_id=source.id,
                        clip_id=source.clip_id,
                        reason="clip_not_production_eligible",
                    )
                )
                continue

            signals, derivation = derive_episode_signals(source.clip_id, features)
            eligible.append(
                ActivationCandidate(
                    discovery_candidate_id=candidate.id,
                    source_item_id=source.id,
                    clip_id=source.clip_id,
                    candidate_score=float(features.candidate_score),
                    signals=signals,
                    derivation=derivation,
                )
            )
            seen_clips.add(source.clip_id)

    ordered_clip_ids: tuple[uuid.UUID, ...] = ()
    ready = len(eligible) >= count
    readiness_reason = "ready" if ready else "insufficient_eligible_clips"
    if ready:
        plan = build_ranking_episode_plan(
            [item.signals.ranking_signals() for item in eligible],
            premise=premise,
            item_count=count,
            contract=contract,
        )
        ordered_clip_ids = tuple(uuid.UUID(item.candidate_id) for item in plan.ordered_items)

    activation_key = _activation_identity(
        channel_profile_id,
        opportunity_id,
        packet.packet_sha256,
        contract.key,
        contract.version,
        count,
    )
    return ActivationPreview(
        channel_profile_id=channel_profile_id,
        trend_opportunity_id=opportunity_id,
        evidence_packet_id=packet.id,
        evidence_packet_sha256=packet.packet_sha256,
        premise=premise,
        format_key=contract.key,
        format_version=contract.version,
        item_count=count,
        activation_key=activation_key,
        ready=ready,
        readiness_reason=readiness_reason,
        eligible=tuple(eligible),
        excluded=tuple(exclusions),
        ordered_clip_ids=ordered_clip_ids,
    )


def activate_trend_opportunity(
    channel_profile_id: uuid.UUID,
    opportunity_id: uuid.UUID,
    *,
    item_count: int | None = None,
    actor: str = "operator",
) -> tuple[ShortEpisode, ActivationPreview]:
    preview = preview_trend_activation(
        channel_profile_id,
        opportunity_id,
        item_count=item_count,
    )
    if not preview.ready:
        raise ValueError(
            f"trend opportunity activation is not ready: {preview.readiness_reason}; "
            f"eligible={len(preview.eligible)} required={preview.item_count}"
        )

    episode = register_short_episode(
        channel_profile_id=channel_profile_id,
        premise=preview.premise,
        candidates=[item.signals for item in preview.eligible],
        item_count=preview.item_count,
        format_key=preview.format_key,
        format_version=preview.format_version,
        idempotency_key=f"trend-activation:{preview.activation_key}",
        trend_opportunity_id=opportunity_id,
        planning_metadata={"trend_activation": preview.snapshot()},
    )
    with session_scope() as session:
        existing = session.scalar(
            select(DomainEvent).where(
                DomainEvent.aggregate_type == "short_episode",
                DomainEvent.aggregate_id == str(episode.id),
                DomainEvent.event_type == "trend.opportunity.activated",
            )
        )
        if existing is None:
            session.add(
                DomainEvent(
                    aggregate_type="short_episode",
                    aggregate_id=str(episode.id),
                    event_type="trend.opportunity.activated",
                    payload={
                        "short_episode_id": str(episode.id),
                        "channel_profile_id": str(channel_profile_id),
                        "trend_opportunity_id": str(opportunity_id),
                        "trend_evidence_packet_id": str(preview.evidence_packet_id),
                        "trend_evidence_packet_sha256": preview.evidence_packet_sha256,
                        "activation_key": preview.activation_key,
                        "eligible_candidate_count": len(preview.eligible),
                        "excluded_candidate_count": len(preview.excluded),
                        "item_count": preview.item_count,
                        "actor": actor,
                    },
                )
            )
    return episode, preview
