from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select

from katcha.db import session_scope
from katcha.intelligence_models import ChannelProfile
from katcha.models import DomainEvent
from katcha.trend_models import (
    ChannelTrendWatchVersion,
    TrendEvidencePacket,
    TrendOpportunity,
    TrendSignal,
    TrendTopic,
    TrendTopicSignal,
)
from katcha.trends.runtime import (
    DEFAULT_BASELINE_WINDOW_HOURS,
    DEFAULT_PREDICTION_HORIZON_HOURS,
    MAX_SIGNALS_PER_TOPIC,
)
from katcha.trends.scoring import (
    SignalSample,
    TopicDescriptor,
    WatchConfig,
    canonical_topic_key,
    score_topic,
)


def _utc(value: datetime | None = None) -> datetime:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _terms(values: list[str] | None) -> list[str]:
    return list(dict.fromkeys(item.strip() for item in (values or []) if item.strip()))


def active_watch_profile(channel_profile_id: uuid.UUID) -> ChannelTrendWatchVersion | None:
    with session_scope() as session:
        return session.scalar(
            select(ChannelTrendWatchVersion)
            .where(ChannelTrendWatchVersion.channel_profile_id == channel_profile_id)
            .order_by(ChannelTrendWatchVersion.version.desc())
            .limit(1)
        )


def create_watch_profile(
    channel_profile_id: uuid.UUID,
    *,
    interests: list[str],
    excluded_terms: list[str] | None = None,
    entities: list[str] | None = None,
    platforms: list[str] | None = None,
    languages: list[str] | None = None,
    regions: list[str] | None = None,
    source_weights: dict[str, float] | None = None,
    freshness_horizon_hours: int = 72,
    min_confidence: float = 0.45,
    opportunity_threshold: float = 0.55,
    metadata: dict[str, Any] | None = None,
    actor: str = "operator",
) -> ChannelTrendWatchVersion:
    with session_scope() as session:
        if session.get(ChannelProfile, channel_profile_id) is None:
            raise ValueError(f"channel profile not found: {channel_profile_id}")
        current = session.scalar(
            select(func.max(ChannelTrendWatchVersion.version)).where(
                ChannelTrendWatchVersion.channel_profile_id == channel_profile_id
            )
        )
        profile = ChannelTrendWatchVersion(
            channel_profile_id=channel_profile_id,
            version=int(current or 0) + 1,
            interests=_terms(interests),
            excluded_terms=_terms(excluded_terms),
            entities=_terms(entities),
            platforms=_terms(platforms),
            languages=_terms(languages),
            regions=_terms(regions),
            source_weights={
                str(key).strip().casefold(): max(0.0, float(value))
                for key, value in (source_weights or {}).items()
                if str(key).strip()
            },
            freshness_horizon_hours=freshness_horizon_hours,
            min_confidence=Decimal(str(min_confidence)),
            opportunity_threshold=Decimal(str(opportunity_threshold)),
            watch_metadata=metadata or {},
        )
        session.add(profile)
        session.flush()
        session.add(
            DomainEvent(
                aggregate_type="channel_trend_watch",
                aggregate_id=str(channel_profile_id),
                event_type="trend.watch_profile.versioned",
                payload={
                    "channel_profile_id": str(channel_profile_id),
                    "version": profile.version,
                    "actor": actor,
                },
            )
        )
        return profile


def register_signal(
    *,
    topic: str,
    provider_key: str,
    external_id: str,
    source_kind: str,
    independence_key: str,
    observed_at: datetime,
    observation_key: str | None = None,
    canonical_url: str | None = None,
    source_name: str | None = None,
    title: str | None = None,
    body_excerpt: str | None = None,
    author: str | None = None,
    community: str | None = None,
    language: str | None = None,
    region: str | None = None,
    published_at: datetime | None = None,
    metrics: dict[str, float] | None = None,
    media_refs: list[dict[str, Any]] | None = None,
    aliases: list[str] | None = None,
    tags: list[str] | None = None,
    content_fingerprint: str | None = None,
    metadata: dict[str, Any] | None = None,
    match_confidence: float = 1.0,
    match_reasons: list[str] | None = None,
) -> TrendSignal:
    observed_at = _utc(observed_at)
    topic_key = canonical_topic_key(topic)
    provider = provider_key.strip().casefold()
    kind = source_kind.strip().casefold()
    independence = independence_key.strip().casefold()
    if not topic_key:
        raise ValueError("topic must contain at least one alphanumeric token")
    if not provider or not external_id.strip() or not kind or not independence:
        raise ValueError(
            "provider_key, external_id, source_kind, and independence_key are required"
        )
    if observation_key is None:
        raw = json.dumps(
            {
                "observed_at": observed_at.isoformat(),
                "metrics": metrics or {},
                "canonical_url": canonical_url,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        observation_key = hashlib.sha256(raw.encode()).hexdigest()[:32]

    with session_scope() as session:
        trend_topic = session.scalar(select(TrendTopic).where(TrendTopic.topic_key == topic_key))
        if trend_topic is None:
            trend_topic = TrendTopic(
                topic_key=topic_key,
                display_name=topic.strip(),
                aliases=_terms(aliases),
                tags=_terms(tags),
                first_seen_at=_utc(published_at or observed_at),
                last_seen_at=observed_at,
            )
            session.add(trend_topic)
            session.flush()
        else:
            trend_topic.last_seen_at = max(_utc(trend_topic.last_seen_at), observed_at)
            trend_topic.aliases = _terms([*trend_topic.aliases, *(aliases or [])])
            trend_topic.tags = _terms([*trend_topic.tags, *(tags or [])])

        signal = session.scalar(
            select(TrendSignal).where(
                TrendSignal.provider_key == provider,
                TrendSignal.external_id == external_id.strip(),
                TrendSignal.observation_key == observation_key,
            )
        )
        if signal is None:
            signal = TrendSignal(
                provider_key=provider,
                external_id=external_id.strip(),
                observation_key=observation_key,
                canonical_url=canonical_url,
                source_kind=kind,
                source_name=source_name,
                independence_key=independence,
                title=title,
                body_excerpt=body_excerpt,
                author=author,
                community=community,
                language=language.casefold() if language else None,
                region=region.casefold() if region else None,
                published_at=_utc(published_at) if published_at else None,
                observed_at=observed_at,
                metrics={str(key): float(value) for key, value in (metrics or {}).items()},
                media_refs=media_refs or [],
                content_fingerprint=content_fingerprint,
                signal_metadata=metadata or {},
            )
            session.add(signal)
            session.flush()

        link = session.scalar(
            select(TrendTopicSignal).where(
                TrendTopicSignal.trend_topic_id == trend_topic.id,
                TrendTopicSignal.trend_signal_id == signal.id,
            )
        )
        if link is None:
            session.add(
                TrendTopicSignal(
                    trend_topic_id=trend_topic.id,
                    trend_signal_id=signal.id,
                    match_confidence=Decimal(str(match_confidence)),
                    match_reasons=match_reasons or ["explicit_topic_assignment"],
                )
            )
            session.add(
                DomainEvent(
                    aggregate_type="trend_topic",
                    aggregate_id=str(trend_topic.id),
                    event_type="trend.signal.observed",
                    payload={
                        "trend_topic_id": str(trend_topic.id),
                        "trend_signal_id": str(signal.id),
                        "provider_key": provider,
                        "source_kind": kind,
                        "observed_at": observed_at.isoformat(),
                    },
                )
            )
        return signal


def _signals_for_topic(
    topic_id: uuid.UUID,
    *,
    cutoff: datetime,
    source_weights: dict[str, float],
) -> tuple[list[TrendSignal], list[SignalSample]]:
    with session_scope() as session:
        signals = list(
            session.scalars(
                select(TrendSignal)
                .join(TrendTopicSignal, TrendTopicSignal.trend_signal_id == TrendSignal.id)
                .where(
                    TrendTopicSignal.trend_topic_id == topic_id,
                    TrendSignal.observed_at >= cutoff,
                )
                .order_by(TrendSignal.observed_at.desc())
                .limit(MAX_SIGNALS_PER_TOPIC)
            )
        )
    samples = [
        SignalSample(
            entity_key=f"{signal.provider_key}:{signal.external_id}",
            source_kind=signal.source_kind,
            independence_key=signal.independence_key,
            observed_at=signal.observed_at,
            published_at=signal.published_at,
            metrics=signal.metrics,
            source_weight=float(source_weights.get(signal.source_kind, 1.0)),
        )
        for signal in signals
    ]
    return signals, samples


def build_evidence_packet(
    opportunity_id: uuid.UUID,
    *,
    signals: list[TrendSignal] | None = None,
) -> TrendEvidencePacket:
    with session_scope() as session:
        opportunity = session.get(TrendOpportunity, opportunity_id)
        if opportunity is None:
            raise ValueError(f"trend opportunity not found: {opportunity_id}")
        topic = session.get(TrendTopic, opportunity.trend_topic_id)
        if topic is None:
            raise ValueError(f"trend topic not found: {opportunity.trend_topic_id}")
        if signals is None:
            signals = list(
                session.scalars(
                    select(TrendSignal)
                    .join(TrendTopicSignal, TrendTopicSignal.trend_signal_id == TrendSignal.id)
                    .where(TrendTopicSignal.trend_topic_id == topic.id)
                    .order_by(TrendSignal.observed_at.desc())
                    .limit(30)
                )
            )

        sources: list[dict[str, Any]] = []
        claims: list[dict[str, Any]] = []
        media_refs: list[dict[str, Any]] = []
        acquisition_refs: list[dict[str, Any]] = []
        seen_media: set[str] = set()
        for signal in sorted(signals, key=lambda item: _utc(item.observed_at), reverse=True)[:30]:
            sources.append(
                {
                    "signal_id": str(signal.id),
                    "provider_key": signal.provider_key,
                    "source_kind": signal.source_kind,
                    "source_name": signal.source_name,
                    "canonical_url": signal.canonical_url,
                    "title": signal.title,
                    "community": signal.community,
                    "published_at": signal.published_at.isoformat()
                    if signal.published_at
                    else None,
                    "observed_at": signal.observed_at.isoformat(),
                    "metrics": signal.metrics,
                }
            )
            if signal.title:
                claims.append(
                    {
                        "text": signal.title,
                        "signal_id": str(signal.id),
                        "source_url": signal.canonical_url,
                        "observed_at": signal.observed_at.isoformat(),
                    }
                )
            for media in signal.media_refs:
                identity = json.dumps(media, sort_keys=True, default=str)
                if identity not in seen_media:
                    seen_media.add(identity)
                    media_refs.append({**media, "signal_id": str(signal.id)})
            for key in ("acquisition_ref", "rights_ref"):
                value = signal.signal_metadata.get(key)
                if value:
                    acquisition_refs.append(
                        {"kind": key, "reference": value, "signal_id": str(signal.id)}
                    )

        payload = {
            "topic_id": str(topic.id),
            "opportunity_id": str(opportunity.id),
            "lifecycle": opportunity.lifecycle,
            "score": float(opportunity.opportunity_score),
            "confidence": float(opportunity.confidence),
            "sources": sources,
            "claims": claims,
            "media_refs": media_refs,
            "acquisition_refs": acquisition_refs,
        }
        packet_sha = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        existing = session.scalar(
            select(TrendEvidencePacket).where(
                TrendEvidencePacket.trend_opportunity_id == opportunity.id,
                TrendEvidencePacket.packet_sha256 == packet_sha,
            )
        )
        if existing is not None:
            return existing
        version = session.scalar(
            select(func.max(TrendEvidencePacket.version)).where(
                TrendEvidencePacket.trend_opportunity_id == opportunity.id
            )
        )
        packet = TrendEvidencePacket(
            trend_opportunity_id=opportunity.id,
            version=int(version or 0) + 1,
            packet_sha256=packet_sha,
            thesis=f"{topic.display_name} is {opportunity.lifecycle} for this channel.",
            why_now=list(opportunity.reasons),
            sources=sources,
            claims=claims,
            media_refs=media_refs,
            acquisition_refs=acquisition_refs,
            packet_metadata={
                "algorithm": "deterministic_trend_packet_v1",
                "opportunity_components": opportunity.components,
            },
        )
        session.add(packet)
        session.flush()
        session.add(
            DomainEvent(
                aggregate_type="trend_opportunity",
                aggregate_id=str(opportunity.id),
                event_type="trend.evidence_packet.ready",
                payload={
                    "trend_opportunity_id": str(opportunity.id),
                    "trend_evidence_packet_id": str(packet.id),
                    "version": packet.version,
                },
            )
        )
        return packet


def refresh_channel_trends(
    channel_profile_id: uuid.UUID,
    *,
    run_key: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = _utc(now)
    with session_scope() as session:
        if session.get(ChannelProfile, channel_profile_id) is None:
            raise ValueError(f"channel profile not found: {channel_profile_id}")
        watch = session.scalar(
            select(ChannelTrendWatchVersion)
            .where(ChannelTrendWatchVersion.channel_profile_id == channel_profile_id)
            .order_by(ChannelTrendWatchVersion.version.desc())
            .limit(1)
        )
        if watch is None:
            raise ValueError(f"trend watch profile not configured: {channel_profile_id}")
        cutoff = now - timedelta(
            hours=max(DEFAULT_BASELINE_WINDOW_HOURS, watch.freshness_horizon_hours)
        )
        topics = list(
            session.scalars(
                select(TrendTopic)
                .where(TrendTopic.last_seen_at >= cutoff)
                .order_by(TrendTopic.last_seen_at.desc())
            )
        )
        config = {
            "version": watch.version,
            "interests": list(watch.interests),
            "excluded_terms": list(watch.excluded_terms),
            "freshness": watch.freshness_horizon_hours,
            "source_weights": dict(watch.source_weights),
            "min_confidence": float(watch.min_confidence),
            "threshold": float(watch.opportunity_threshold),
        }

    rows: list[tuple[TrendOpportunity, list[TrendSignal]]] = []
    for topic in topics:
        signals, samples = _signals_for_topic(
            topic.id,
            cutoff=cutoff,
            source_weights=config["source_weights"],
        )
        if not samples:
            continue
        result = score_topic(
            topic=TopicDescriptor(topic.display_name, topic.aliases, topic.tags),
            samples=samples,
            watch=WatchConfig(
                interests=config["interests"],
                excluded_terms=config["excluded_terms"],
                freshness_horizon_hours=config["freshness"],
            ),
            now=now,
        )
        with session_scope() as session:
            opportunity = session.scalar(
                select(TrendOpportunity).where(
                    TrendOpportunity.channel_profile_id == channel_profile_id,
                    TrendOpportunity.trend_topic_id == topic.id,
                    TrendOpportunity.run_key == run_key,
                )
            )
            if opportunity is None:
                opportunity = TrendOpportunity(
                    channel_profile_id=channel_profile_id,
                    trend_topic_id=topic.id,
                    watch_version=config["version"],
                    run_key=run_key,
                    lifecycle=result.lifecycle,
                    opportunity_score=Decimal(str(result.opportunity_score)),
                    confidence=Decimal(str(result.confidence)),
                    prediction_horizon_hours=DEFAULT_PREDICTION_HORIZON_HOURS,
                    expires_at=now + timedelta(hours=config["freshness"]),
                    components=result.components,
                    reasons=list(result.reasons),
                    evidence_summary={
                        "signal_count": len(signals),
                        "source_kinds": sorted({item.source_kind for item in signals}),
                        "independent_sources": len(
                            {item.independence_key for item in signals}
                        ),
                    },
                )
                session.add(opportunity)
                session.flush()
            rows.append((opportunity, signals))

    rows.sort(
        key=lambda row: (float(row[0].opportunity_score), float(row[0].confidence)),
        reverse=True,
    )
    qualified: list[tuple[uuid.UUID, list[TrendSignal]]] = []
    with session_scope() as session:
        for rank, (opportunity, signals) in enumerate(rows, start=1):
            stored = session.get(TrendOpportunity, opportunity.id)
            if stored is None:
                continue
            stored.rank = rank
            if (
                float(stored.opportunity_score) >= config["threshold"]
                and float(stored.confidence) >= config["min_confidence"]
            ):
                qualified.append((stored.id, signals))
                session.add(
                    DomainEvent(
                        aggregate_type="trend_opportunity",
                        aggregate_id=str(stored.id),
                        event_type=f"trend.opportunity.{stored.lifecycle}",
                        payload={
                            "trend_opportunity_id": str(stored.id),
                            "channel_profile_id": str(channel_profile_id),
                            "trend_topic_id": str(stored.trend_topic_id),
                            "score": float(stored.opportunity_score),
                            "confidence": float(stored.confidence),
                            "rank": rank,
                            "run_key": run_key,
                        },
                    )
                )

    for opportunity_id, signals in qualified:
        build_evidence_packet(opportunity_id, signals=signals)
    return {
        "channel_profile_id": str(channel_profile_id),
        "run_key": run_key,
        "watch_version": config["version"],
        "topics_scored": len(rows),
        "qualified_opportunities": len(qualified),
        "evidence_packets_built": len(qualified),
    }


def list_opportunities(
    channel_profile_id: uuid.UUID,
    *,
    limit: int = 25,
    min_score: float = 0.0,
) -> list[TrendOpportunity]:
    with session_scope() as session:
        return list(
            session.scalars(
                select(TrendOpportunity)
                .where(
                    TrendOpportunity.channel_profile_id == channel_profile_id,
                    TrendOpportunity.opportunity_score >= Decimal(str(min_score)),
                    TrendOpportunity.expires_at > datetime.now(timezone.utc),
                )
                .order_by(
                    TrendOpportunity.created_at.desc(),
                    TrendOpportunity.rank.asc(),
                    TrendOpportunity.opportunity_score.desc(),
                )
                .limit(limit)
            )
        )


def latest_evidence_packet(opportunity_id: uuid.UUID) -> TrendEvidencePacket | None:
    with session_scope() as session:
        return session.scalar(
            select(TrendEvidencePacket)
            .where(TrendEvidencePacket.trend_opportunity_id == opportunity_id)
            .order_by(TrendEvidencePacket.version.desc())
            .limit(1)
        )
