from __future__ import annotations

import uuid
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select

from katcha.db import session_scope
from katcha.intelligence.scheduling import HistoricalSlot, recommend_windows
from katcha.intelligence_models import ScheduleRecommendation
from katcha.models import DomainEvent
from katcha.publishing_models import Publication
from katcha.services.channel_learning import latest_observations
from katcha.services.channel_profiles import active_strategy, ensure_active_profile


def _blackout_set(items: list[dict[str, object]]) -> set[tuple[int, int]]:
    result: set[tuple[int, int]] = set()
    for raw in items:
        try:
            result.add((int(raw["weekday"]), int(raw["hour_local"])))
        except (KeyError, TypeError, ValueError):
            continue
    return result


def compute_schedule_recommendations(
    channel_profile_id: uuid.UUID,
    *,
    run_key: str,
    limit: int = 5,
) -> list[ScheduleRecommendation]:
    if limit < 1 or limit > 20:
        raise ValueError("schedule recommendation limit must be between 1 and 20")

    with session_scope() as session:
        profile = ensure_active_profile(session, channel_profile_id)
        existing = list(
            session.scalars(
                select(ScheduleRecommendation)
                .where(
                    ScheduleRecommendation.channel_profile_id == profile.id,
                    ScheduleRecommendation.run_key == run_key,
                )
                .order_by(ScheduleRecommendation.rank)
            )
        )
        if existing:
            for item in existing:
                session.expunge(item)
            return existing

        strategy = active_strategy(session, profile)
        observations = latest_observations(session, profile.id)
        publication_ids = [item.publication_id for item in observations]
        publications = {
            item.id: item
            for item in session.scalars(
                select(Publication).where(Publication.id.in_(publication_ids))
            )
        }
        timezone = ZoneInfo(profile.timezone)
        history: list[HistoricalSlot] = []
        for observation in observations:
            publication = publications.get(observation.publication_id)
            if publication is None:
                continue
            anchor = (
                publication.published_at
                or publication.publish_at
                or publication.created_at
            )
            local = anchor.astimezone(timezone)
            history.append(
                HistoricalSlot(
                    weekday=local.weekday(),
                    hour_local=local.hour,
                    outcome_score=float(observation.outcome_score),
                )
            )

        blackouts = _blackout_set(list(strategy.blackout_windows or []))
        fallback = [
            item
            for item in list(strategy.fallback_schedule or [])
            if (
                int(item.get("weekday", -1)),
                int(item.get("hour_local", -1)),
            )
            not in blackouts
        ]
        requested = min(20, limit + len(blackouts))
        raw = recommend_windows(
            history,
            fallback_schedule=fallback,
            limit=requested,
        )
        selected = [
            item
            for item in raw
            if (item.weekday, item.hour_local) not in blackouts
        ][:limit]

        persisted: list[ScheduleRecommendation] = []
        for rank, item in enumerate(selected, start=1):
            row = ScheduleRecommendation(
                channel_profile_id=profile.id,
                run_key=run_key,
                rank=rank,
                weekday=item.weekday,
                hour_local=item.hour_local,
                score=Decimal(str(item.score)),
                sample_count=item.sample_count,
                confidence=Decimal(str(item.confidence)),
                source=item.source,
                recommendation_metadata={
                    "timezone": profile.timezone,
                    "strategy_version": strategy.version,
                    "history_count": len(history),
                },
            )
            session.add(row)
            persisted.append(row)

        session.add(
            DomainEvent(
                aggregate_type="channel_profile",
                aggregate_id=str(profile.id),
                event_type="channel_profile.schedule_refreshed",
                payload={
                    "channel_profile_id": str(profile.id),
                    "run_key": run_key,
                    "recommendation_count": len(persisted),
                    "history_count": len(history),
                    "timezone": profile.timezone,
                },
            )
        )
        session.flush()
        for item in persisted:
            session.refresh(item)
            session.expunge(item)
        return persisted


def latest_schedule_recommendations(
    channel_profile_id: uuid.UUID,
    *,
    limit: int = 5,
) -> list[ScheduleRecommendation]:
    with session_scope() as session:
        profile = ensure_active_profile(session, channel_profile_id)
        latest_key = session.scalar(
            select(ScheduleRecommendation.run_key)
            .where(ScheduleRecommendation.channel_profile_id == profile.id)
            .order_by(ScheduleRecommendation.created_at.desc())
            .limit(1)
        )
        if latest_key is None:
            return []
        rows = list(
            session.scalars(
                select(ScheduleRecommendation)
                .where(
                    ScheduleRecommendation.channel_profile_id == profile.id,
                    ScheduleRecommendation.run_key == latest_key,
                )
                .order_by(ScheduleRecommendation.rank)
                .limit(limit)
            )
        )
        for row in rows:
            session.expunge(row)
        return rows
