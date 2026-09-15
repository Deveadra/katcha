from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session

from katcha.db import session_scope
from katcha.intelligence_models import ChannelEconomicsSnapshot, ChannelProfile
from katcha.integrations.youtube.oauth import MONETARY_SCOPE
from katcha.longform_models import Compilation, CompilationSegment
from katcha.models import ClipAnalysisRun, DomainEvent, UsageEvent
from katcha.production_models import Production
from katcha.publishing_models import (
    Publication,
    PublicationAnalyticsSnapshot,
    YouTubeConnection,
)
from katcha.services.channel_profiles import active_strategy, ensure_active_profile


def _decimal(value: Decimal | float | int | str) -> Decimal:
    return Decimal(str(value))


def _month_start(now: datetime) -> datetime:
    return datetime(now.year, now.month, 1, tzinfo=UTC)


def _latest_snapshots(
    session: Session,
    publication_ids: list[uuid.UUID],
) -> dict[uuid.UUID, PublicationAnalyticsSnapshot]:
    if not publication_ids:
        return {}
    rows = list(
        session.scalars(
            select(PublicationAnalyticsSnapshot)
            .where(PublicationAnalyticsSnapshot.publication_id.in_(publication_ids))
            .order_by(PublicationAnalyticsSnapshot.sampled_at.desc())
        )
    )
    latest: dict[uuid.UUID, PublicationAnalyticsSnapshot] = {}
    for row in rows:
        latest.setdefault(row.publication_id, row)
    return latest


def _usage_cost(
    session: Session,
    *,
    reference_type: str,
    reference_ids: list[str],
    since: datetime,
) -> Decimal:
    if not reference_ids:
        return Decimal("0")
    value = session.scalar(
        select(func.coalesce(func.sum(UsageEvent.cost_usd), 0)).where(
            UsageEvent.reference_type == reference_type,
            UsageEvent.reference_id.in_(reference_ids),
            UsageEvent.created_at >= since,
        )
    )
    return _decimal(value or 0)


def _source_connection_count(
    session: Session,
    *,
    source_kind: str,
    source_id: uuid.UUID,
) -> int:
    column = (
        Publication.production_id
        if source_kind == "production"
        else Publication.compilation_id
    )
    value = session.scalar(
        select(func.count(distinct(Publication.youtube_connection_id))).where(
            column == source_id
        )
    )
    return max(int(value or 0), 1)


def _source_cost(
    session: Session,
    *,
    source_kind: str,
    source_id: uuid.UUID,
    channel_profile_id: uuid.UUID,
    since: datetime,
) -> Decimal:
    source = (
        session.get(Production, source_id)
        if source_kind == "production"
        else session.get(Compilation, source_id)
    )
    if source is None:
        return Decimal("0")
    scoped_profile_id = getattr(source, "channel_profile_id", None)
    if scoped_profile_id is not None and scoped_profile_id != channel_profile_id:
        return Decimal("0")

    total = _usage_cost(
        session,
        reference_type=source_kind,
        reference_ids=[str(source_id)],
        since=since,
    )
    if scoped_profile_id == channel_profile_id:
        return total
    divisor = _source_connection_count(
        session,
        source_kind=source_kind,
        source_id=source_id,
    )
    return total / Decimal(divisor)


def _channel_clip_ids(
    session: Session,
    publications: list[Publication],
) -> set[uuid.UUID]:
    production_ids = [
        item.production_id
        for item in publications
        if item.production_id is not None
    ]
    compilation_ids = [
        item.compilation_id
        for item in publications
        if item.compilation_id is not None
    ]
    clip_ids: set[uuid.UUID] = set()
    if production_ids:
        clip_ids.update(
            session.scalars(
                select(Production.clip_id).where(Production.id.in_(production_ids))
            )
        )
    if compilation_ids:
        clip_ids.update(
            session.scalars(
                select(CompilationSegment.clip_id).where(
                    CompilationSegment.compilation_id.in_(compilation_ids)
                )
            )
        )
    return clip_ids


def _clip_connection_count(session: Session, clip_id: uuid.UUID) -> int:
    direct = set(
        session.scalars(
            select(Publication.youtube_connection_id)
            .join(Production, Publication.production_id == Production.id)
            .where(Production.clip_id == clip_id)
        )
    )
    compilation = set(
        session.scalars(
            select(Publication.youtube_connection_id)
            .join(
                CompilationSegment,
                Publication.compilation_id == CompilationSegment.compilation_id,
            )
            .where(CompilationSegment.clip_id == clip_id)
        )
    )
    return max(len(direct | compilation), 1)


def _shared_analysis_cost(
    session: Session,
    *,
    clip_ids: set[uuid.UUID],
    since: datetime,
) -> Decimal:
    total = Decimal("0")
    for clip_id in clip_ids:
        run_ids = [
            str(value)
            for value in session.scalars(
                select(ClipAnalysisRun.id).where(ClipAnalysisRun.clip_id == clip_id)
            )
        ]
        if not run_ids:
            continue
        cost = _usage_cost(
            session,
            reference_type="analysis_run",
            reference_ids=run_ids,
            since=since,
        )
        total += cost / Decimal(_clip_connection_count(session, clip_id))
    return total


def compute_channel_economics(
    channel_profile_id: uuid.UUID,
    *,
    sample_key: str,
    sampled_at: datetime | None = None,
) -> ChannelEconomicsSnapshot:
    now = (sampled_at or datetime.now(UTC)).astimezone(UTC)
    since = _month_start(now)
    with session_scope() as session:
        profile = ensure_active_profile(session, channel_profile_id)
        existing = session.scalar(
            select(ChannelEconomicsSnapshot).where(
                ChannelEconomicsSnapshot.channel_profile_id == profile.id,
                ChannelEconomicsSnapshot.sample_key == sample_key,
            )
        )
        if existing is not None:
            session.expunge(existing)
            return existing

        strategy = active_strategy(session, profile)
        connection = session.get(YouTubeConnection, profile.youtube_connection_id)
        if connection is None:
            raise RuntimeError("channel profile YouTube connection disappeared")
        publications = list(
            session.scalars(
                select(Publication).where(
                    Publication.youtube_connection_id == profile.youtube_connection_id
                )
            )
        )
        publication_ids = [item.id for item in publications]
        latest = _latest_snapshots(session, publication_ids)
        revenue = sum(
            (
                _decimal(snapshot.estimated_revenue)
                for snapshot in latest.values()
                if snapshot.estimated_revenue is not None
            ),
            Decimal("0"),
        )

        source_cost = Decimal("0")
        seen_sources: set[tuple[str, uuid.UUID]] = set()
        for publication in publications:
            if publication.production_id is not None:
                key = ("production", publication.production_id)
            elif publication.compilation_id is not None:
                key = ("compilation", publication.compilation_id)
            else:
                continue
            if key in seen_sources:
                continue
            seen_sources.add(key)
            source_cost += _source_cost(
                session,
                source_kind=key[0],
                source_id=key[1],
                channel_profile_id=profile.id,
                since=since,
            )

        shared_analysis_cost = _shared_analysis_cost(
            session,
            clip_ids=_channel_clip_ids(session, publications),
            since=since,
        )
        spend = source_cost + shared_analysis_cost
        margin = revenue - spend
        reinvestable = min(
            max(margin, Decimal("0")) * strategy.reinvestment_rate,
            strategy.reinvestment_cap_usd,
        )
        effective_ceiling = strategy.monthly_hard_budget_usd + reinvestable
        headroom = max(effective_ceiling - spend, Decimal("0"))
        monetary_scope = MONETARY_SCOPE in set(connection.scopes or [])

        snapshot = ChannelEconomicsSnapshot(
            channel_profile_id=profile.id,
            sample_key=sample_key,
            sampled_at=now,
            revenue_usd=revenue,
            attributed_ai_cost_usd=spend,
            contribution_margin_usd=margin,
            reinvestable_usd=reinvestable,
            hard_budget_usd=strategy.monthly_hard_budget_usd,
            month_to_date_spend_usd=spend,
            budget_headroom_usd=headroom,
            monetary_scope_available=monetary_scope,
            details={
                "source_cost_usd": str(source_cost),
                "shared_analysis_cost_usd": str(shared_analysis_cost),
                "effective_spend_ceiling_usd": str(effective_ceiling),
                "publication_count": len(publications),
                "revenue_publication_count": sum(
                    snapshot.estimated_revenue is not None
                    for snapshot in latest.values()
                ),
                "strategy_version": strategy.version,
            },
        )
        session.add(snapshot)
        session.add(
            DomainEvent(
                aggregate_type="channel_profile",
                aggregate_id=str(profile.id),
                event_type="channel_profile.economics_refreshed",
                payload={
                    "channel_profile_id": str(profile.id),
                    "sample_key": sample_key,
                    "revenue_usd": str(revenue),
                    "spend_usd": str(spend),
                    "margin_usd": str(margin),
                    "reinvestable_usd": str(reinvestable),
                    "budget_headroom_usd": str(headroom),
                },
            )
        )
        session.flush()
        session.refresh(snapshot)
        session.expunge(snapshot)
        return snapshot


def latest_economics_snapshot(
    session: Session,
    profile: ChannelProfile,
) -> ChannelEconomicsSnapshot | None:
    return session.scalar(
        select(ChannelEconomicsSnapshot)
        .where(ChannelEconomicsSnapshot.channel_profile_id == profile.id)
        .order_by(ChannelEconomicsSnapshot.sampled_at.desc())
        .limit(1)
    )
