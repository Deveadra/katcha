from __future__ import annotations

import uuid
from calendar import monthrange
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session

from katcha.db import session_scope
from katcha.integrations.youtube.oauth import MONETARY_SCOPE
from katcha.intelligence_models import (
    AIBudgetReservation,
    ChannelEconomicsSnapshot,
    ChannelProfile,
)
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


def _legacy_source_cost(
    session: Session,
    *,
    source_kind: str,
    source_id: uuid.UUID,
    since: datetime,
) -> Decimal:
    total = _usage_cost(
        session,
        reference_type=source_kind,
        reference_ids=[str(source_id)],
        since=since,
    )
    return total / Decimal(
        _source_connection_count(
            session,
            source_kind=source_kind,
            source_id=source_id,
        )
    )


def _scoped_source_ids(
    session: Session,
    channel_profile_id: uuid.UUID,
) -> tuple[set[uuid.UUID], set[uuid.UUID]]:
    production_ids = set(
        session.scalars(
            select(Production.id).where(
                Production.channel_profile_id == channel_profile_id
            )
        )
    )
    compilation_ids = set(
        session.scalars(
            select(Compilation.id).where(
                Compilation.channel_profile_id == channel_profile_id
            )
        )
    )
    return production_ids, compilation_ids


def _channel_publications(
    session: Session,
    profile: ChannelProfile,
) -> list[Publication]:
    return list(
        session.scalars(
            select(Publication).where(
                Publication.youtube_connection_id == profile.youtube_connection_id
            )
        )
    )


def _channel_clip_ids(
    session: Session,
    *,
    production_ids: set[uuid.UUID],
    compilation_ids: set[uuid.UUID],
    publications: list[Publication],
) -> set[uuid.UUID]:
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

    published_production_ids = {
        item.production_id
        for item in publications
        if item.production_id is not None
    }
    published_compilation_ids = {
        item.compilation_id
        for item in publications
        if item.compilation_id is not None
    }
    if published_production_ids:
        clip_ids.update(
            session.scalars(
                select(Production.clip_id).where(
                    Production.id.in_(published_production_ids)
                )
            )
        )
    if published_compilation_ids:
        clip_ids.update(
            session.scalars(
                select(CompilationSegment.clip_id).where(
                    CompilationSegment.compilation_id.in_(
                        published_compilation_ids
                    )
                )
            )
        )
    return clip_ids


def _clip_channel_count(session: Session, clip_id: uuid.UUID) -> int:
    keys: set[str] = set()
    for profile_id in session.scalars(
        select(Production.channel_profile_id).where(
            Production.clip_id == clip_id,
            Production.channel_profile_id.is_not(None),
        )
    ):
        if profile_id is not None:
            keys.add(f"profile:{profile_id}")

    for profile_id in session.scalars(
        select(Compilation.channel_profile_id)
        .join(
            CompilationSegment,
            CompilationSegment.compilation_id == Compilation.id,
        )
        .where(
            CompilationSegment.clip_id == clip_id,
            Compilation.channel_profile_id.is_not(None),
        )
    ):
        if profile_id is not None:
            keys.add(f"profile:{profile_id}")

    connection_ids = set(
        session.scalars(
            select(Publication.youtube_connection_id)
            .join(Production, Publication.production_id == Production.id)
            .where(Production.clip_id == clip_id)
        )
    )
    connection_ids.update(
        session.scalars(
            select(Publication.youtube_connection_id)
            .join(
                CompilationSegment,
                Publication.compilation_id == CompilationSegment.compilation_id,
            )
            .where(CompilationSegment.clip_id == clip_id)
        )
    )
    if connection_ids:
        profile_by_connection = {
            connection_id: profile_id
            for connection_id, profile_id in session.execute(
                select(
                    ChannelProfile.youtube_connection_id,
                    ChannelProfile.id,
                ).where(ChannelProfile.youtube_connection_id.in_(connection_ids))
            )
        }
        for connection_id in connection_ids:
            profile_id = profile_by_connection.get(connection_id)
            key = (
                f"profile:{profile_id}"
                if profile_id is not None
                else f"connection:{connection_id}"
            )
            keys.add(key)
    return max(len(keys), 1)


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
        total += cost / Decimal(_clip_channel_count(session, clip_id))
    return total


def _actual_spend_breakdown(
    session: Session,
    *,
    profile: ChannelProfile,
    since: datetime,
    publications: list[Publication],
) -> tuple[Decimal, Decimal, Decimal]:
    production_ids, compilation_ids = _scoped_source_ids(session, profile.id)
    scoped_source_cost = _usage_cost(
        session,
        reference_type="production",
        reference_ids=[str(value) for value in production_ids],
        since=since,
    ) + _usage_cost(
        session,
        reference_type="compilation",
        reference_ids=[str(value) for value in compilation_ids],
        since=since,
    )

    legacy_source_cost = Decimal("0")
    seen_legacy: set[tuple[str, uuid.UUID]] = set()
    for publication in publications:
        if publication.production_id is not None:
            source_kind = "production"
            source_id = publication.production_id
            source = session.get(Production, source_id)
        elif publication.compilation_id is not None:
            source_kind = "compilation"
            source_id = publication.compilation_id
            source = session.get(Compilation, source_id)
        else:
            continue
        if source is None or source.channel_profile_id is not None:
            continue
        key = (source_kind, source_id)
        if key in seen_legacy:
            continue
        seen_legacy.add(key)
        legacy_source_cost += _legacy_source_cost(
            session,
            source_kind=source_kind,
            source_id=source_id,
            since=since,
        )

    clip_ids = _channel_clip_ids(
        session,
        production_ids=production_ids,
        compilation_ids=compilation_ids,
        publications=publications,
    )
    shared_analysis_cost = _shared_analysis_cost(
        session,
        clip_ids=clip_ids,
        since=since,
    )
    source_cost = scoped_source_cost + legacy_source_cost
    return source_cost + shared_analysis_cost, source_cost, shared_analysis_cost


def _month_to_date_revenue(
    session: Session,
    *,
    publications: list[Publication],
    since: datetime,
    now: datetime,
) -> tuple[Decimal, int, int]:
    publication_ids = [item.id for item in publications]
    if not publication_ids:
        return Decimal("0"), 0, 0
    snapshots = list(
        session.scalars(
            select(PublicationAnalyticsSnapshot)
            .where(
                PublicationAnalyticsSnapshot.publication_id.in_(publication_ids),
                PublicationAnalyticsSnapshot.sampled_at <= now,
            )
            .order_by(PublicationAnalyticsSnapshot.sampled_at.asc())
        )
    )
    by_publication: dict[uuid.UUID, list[PublicationAnalyticsSnapshot]] = {}
    for snapshot in snapshots:
        by_publication.setdefault(snapshot.publication_id, []).append(snapshot)

    revenue = Decimal("0")
    usable = 0
    incomplete = 0
    for publication in publications:
        rows = by_publication.get(publication.id, [])
        monetary = [row for row in rows if row.estimated_revenue is not None]
        if not monetary:
            continue
        latest = monetary[-1]
        latest_revenue = _decimal(latest.estimated_revenue or 0)
        anchor = publication.published_at or publication.publish_at or publication.created_at
        if anchor >= since:
            revenue += max(latest_revenue, Decimal("0"))
            usable += 1
            continue
        prior = [row for row in monetary if row.sampled_at < since]
        if not prior:
            incomplete += 1
            continue
        baseline = _decimal(prior[-1].estimated_revenue or 0)
        revenue += max(latest_revenue - baseline, Decimal("0"))
        usable += 1
    return revenue, usable, incomplete


def active_reserved_cost(
    session: Session,
    channel_profile_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> Decimal:
    timestamp = (now or datetime.now(UTC)).astimezone(UTC)
    value = session.scalar(
        select(func.coalesce(func.sum(AIBudgetReservation.estimated_cost_usd), 0)).where(
            AIBudgetReservation.channel_profile_id == channel_profile_id,
            AIBudgetReservation.status == "reserved",
            AIBudgetReservation.expires_at > timestamp,
        )
    )
    return _decimal(value or 0)


def budget_state(
    session: Session,
    profile: ChannelProfile,
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    timestamp = (now or datetime.now(UTC)).astimezone(UTC)
    since = _month_start(timestamp)
    strategy = active_strategy(session, profile)
    connection = session.get(YouTubeConnection, profile.youtube_connection_id)
    if connection is None:
        raise RuntimeError("channel profile YouTube connection disappeared")
    publications = _channel_publications(session, profile)
    revenue, revenue_count, revenue_incomplete = _month_to_date_revenue(
        session,
        publications=publications,
        since=since,
        now=timestamp,
    )
    spend, source_cost, shared_analysis_cost = _actual_spend_breakdown(
        session,
        profile=profile,
        since=since,
        publications=publications,
    )
    reserved = active_reserved_cost(session, profile.id, now=timestamp)
    margin = revenue - spend
    reinvestable = min(
        max(margin, Decimal("0")) * strategy.reinvestment_rate,
        strategy.reinvestment_cap_usd,
    )
    effective_budget = min(
        strategy.monthly_hard_budget_usd,
        strategy.monthly_base_budget_usd + reinvestable,
    )
    headroom = max(effective_budget - spend - reserved, Decimal("0"))
    elapsed_days = max((timestamp - since).total_seconds() / 86400.0, 1.0)
    burn_rate = spend / Decimal(str(elapsed_days))
    days_in_month = monthrange(timestamp.year, timestamp.month)[1]
    projected = burn_rate * Decimal(days_in_month)
    monetary_scope = MONETARY_SCOPE in set(connection.scopes or [])
    return {
        "sampled_at": timestamp,
        "revenue_usd": revenue,
        "attributed_ai_cost_usd": spend,
        "contribution_margin_usd": margin,
        "reinvestable_usd": reinvestable,
        "base_budget_usd": strategy.monthly_base_budget_usd,
        "hard_budget_usd": strategy.monthly_hard_budget_usd,
        "effective_budget_usd": effective_budget,
        "month_to_date_spend_usd": spend,
        "reserved_ai_cost_usd": reserved,
        "budget_headroom_usd": headroom,
        "burn_rate_usd_per_day": burn_rate,
        "projected_month_end_spend_usd": projected,
        "monetary_scope_available": monetary_scope,
        "details": {
            "source_cost_usd": str(source_cost),
            "shared_analysis_cost_usd": str(shared_analysis_cost),
            "publication_count": len(publications),
            "revenue_publication_count": revenue_count,
            "revenue_incomplete_publication_count": revenue_incomplete,
            "strategy_version": strategy.version,
            "reinvestment_is_hard_capped": True,
        },
    }


def compute_channel_economics(
    channel_profile_id: uuid.UUID,
    *,
    sample_key: str,
    sampled_at: datetime | None = None,
) -> ChannelEconomicsSnapshot:
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
        state = budget_state(session, profile, now=sampled_at)
        snapshot = ChannelEconomicsSnapshot(
            channel_profile_id=profile.id,
            sample_key=sample_key,
            sampled_at=state["sampled_at"],
            revenue_usd=state["revenue_usd"],
            attributed_ai_cost_usd=state["attributed_ai_cost_usd"],
            contribution_margin_usd=state["contribution_margin_usd"],
            reinvestable_usd=state["reinvestable_usd"],
            base_budget_usd=state["base_budget_usd"],
            hard_budget_usd=state["hard_budget_usd"],
            effective_budget_usd=state["effective_budget_usd"],
            month_to_date_spend_usd=state["month_to_date_spend_usd"],
            reserved_ai_cost_usd=state["reserved_ai_cost_usd"],
            budget_headroom_usd=state["budget_headroom_usd"],
            burn_rate_usd_per_day=state["burn_rate_usd_per_day"],
            projected_month_end_spend_usd=state["projected_month_end_spend_usd"],
            monetary_scope_available=bool(state["monetary_scope_available"]),
            details=dict(state["details"]),
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
                    "revenue_usd": str(state["revenue_usd"]),
                    "spend_usd": str(state["attributed_ai_cost_usd"]),
                    "reserved_usd": str(state["reserved_ai_cost_usd"]),
                    "margin_usd": str(state["contribution_margin_usd"]),
                    "reinvestable_usd": str(state["reinvestable_usd"]),
                    "effective_budget_usd": str(state["effective_budget_usd"]),
                    "budget_headroom_usd": str(state["budget_headroom_usd"]),
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
