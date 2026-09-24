"""Read models shared by the explorer and external AI controllers."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select

from katcha.db import session_scope
from katcha.services.channel_profiles import ensure_active_profile
from katcha.services.trends import _signal_allowed
from katcha.trend_models import (
    ChannelTrendWatchVersion,
    TrendEvidencePacket,
    TrendOpportunity,
    TrendSignal,
    TrendTopic,
    TrendTopicSignal,
)


def opportunity_board(channel_id: uuid.UUID, limit: int = 50) -> list[dict]:
    """One current snapshot per topic, using the active channel watch version."""
    with session_scope() as session:
        ensure_active_profile(session, channel_id)
        watch = session.scalar(
            select(ChannelTrendWatchVersion)
            .where(ChannelTrendWatchVersion.channel_profile_id == channel_id)
            .order_by(ChannelTrendWatchVersion.version.desc())
            .limit(1)
        )
        if watch is None:
            return []
        ranked = (
            select(
                TrendOpportunity.id.label("id"),
                func.row_number()
                .over(
                    partition_by=TrendOpportunity.trend_topic_id,
                    order_by=(TrendOpportunity.created_at.desc(), TrendOpportunity.id.desc()),
                )
                .label("position"),
            )
            .where(
                TrendOpportunity.channel_profile_id == channel_id,
                TrendOpportunity.watch_version == watch.version,
            )
            .subquery()
        )
        rows = session.execute(
            select(TrendOpportunity, TrendTopic)
            .join(ranked, ranked.c.id == TrendOpportunity.id)
            .join(TrendTopic, TrendTopic.id == TrendOpportunity.trend_topic_id)
            .where(ranked.c.position == 1, TrendOpportunity.expires_at > datetime.now(UTC))
            .order_by(
                func.coalesce(
                    TrendOpportunity.calibrated_score, TrendOpportunity.opportunity_score
                ).desc(),
                TrendOpportunity.confidence.desc(),
                TrendOpportunity.id,
            )
            .limit(limit)
        ).all()
        return [
            {"topic": topic.display_name, "tags": topic.tags, "opportunity": opportunity}
            for opportunity, topic in rows
        ]


def opportunity_dossier(
    channel_id: uuid.UUID, opportunity_id: uuid.UUID, *, hours: int = 168, limit: int = 500
) -> dict:
    with session_scope() as session:
        ensure_active_profile(session, channel_id)
        opportunity = session.get(TrendOpportunity, opportunity_id)
        if opportunity is None or opportunity.channel_profile_id != channel_id:
            raise ValueError("trend opportunity not found in this channel")
        topic = session.get(TrendTopic, opportunity.trend_topic_id)
        watch = session.scalar(
            select(ChannelTrendWatchVersion).where(
                ChannelTrendWatchVersion.channel_profile_id == channel_id,
                ChannelTrendWatchVersion.version == opportunity.watch_version,
            )
        )
        if watch is None or topic is None:
            raise ValueError("trend snapshot dependencies not found")
        packet = session.scalar(
            select(TrendEvidencePacket)
            .where(TrendEvidencePacket.trend_opportunity_id == opportunity_id)
            .order_by(TrendEvidencePacket.version.desc())
            .limit(1)
        )
        cutoff = opportunity.created_at - timedelta(hours=hours)
        stmt = (
            select(TrendSignal)
            .join(TrendTopicSignal, TrendTopicSignal.trend_signal_id == TrendSignal.id)
            .where(
                TrendTopicSignal.trend_topic_id == opportunity.trend_topic_id,
                TrendSignal.observed_at >= cutoff,
                TrendSignal.observed_at <= opportunity.created_at,
            )
        )
        # Filter before LIMIT so unrelated platform/locale data cannot hide eligible signals.
        if watch.platforms:
            stmt = stmt.where(
                or_(
                    func.lower(TrendSignal.provider_key).in_(
                        [x.casefold() for x in watch.platforms]
                    ),
                    func.lower(TrendSignal.source_kind).in_(
                        [x.casefold() for x in watch.platforms]
                    ),
                )
            )
        if watch.languages:
            stmt = stmt.where(
                func.lower(TrendSignal.language).in_([x.casefold() for x in watch.languages])
            )
        if watch.regions:
            stmt = stmt.where(
                func.lower(TrendSignal.region).in_([x.casefold() for x in watch.regions])
            )
        signals = list(
            session.scalars(
                stmt.order_by(TrendSignal.observed_at.desc(), TrendSignal.id.desc()).limit(
                    limit + 1
                )
            )
        )
        signals = [
            s
            for s in signals
            if _signal_allowed(
                s,
                platforms={x.casefold() for x in watch.platforms},
                languages={x.casefold() for x in watch.languages},
                regions={x.casefold() for x in watch.regions},
            )
        ]
        return {
            "topic": topic.display_name,
            "tags": topic.tags,
            "opportunity": opportunity,
            "evidence": packet,
            "signals": signals[:limit],
            "truncated": len(signals) > limit,
            "window_start": cutoff,
            "window_end": opportunity.created_at,
        }
