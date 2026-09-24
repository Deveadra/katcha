from __future__ import annotations

import uuid
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select, text

from katcha.acquisition_models import DiscoveryRun
from katcha.config import get_settings
from katcha.db import session_scope
from katcha.models import DomainEvent


class DiscoveryQuotaExhausted(Exception):
    pass


def reserve_youtube_page(run_id: uuid.UUID, *, now: datetime | None = None) -> dict[str, object]:
    """Conservatively reserve one search and one optional details call before polling.

    A reservation is never refunded: the remote request may have succeeded before
    persistence failed. Temporal replay can therefore overcount, but not overspend
    this app's discovery budget. Other consumers of the API key are not included.
    """
    settings = get_settings()
    reference = now or datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    day = reference.astimezone(ZoneInfo("America/Los_Angeles")).date().isoformat()
    with session_scope() as session:
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            # Fixed app-wide key serializes the daily budget across workers/channels.
            session.execute(text("SELECT pg_advisory_xact_lock(47008531)"))
        run = session.get(DiscoveryRun, run_id)
        if run is None:
            raise ValueError(f"discovery run not found: {run_id}")
        if run.adapter_key != "youtube":
            raise ValueError("YouTube quota guard requires a YouTube discovery run")
        reservations = list(
            session.scalars(
                select(DiscoveryRun).where(
                    DiscoveryRun.adapter_key == "youtube",
                    DiscoveryRun.run_metadata["youtube_quota_day"].as_string() == day,
                )
            )
        )
        used_search = sum(
            int(item.run_metadata.get("youtube_search_reserved", 0)) for item in reservations
        )
        used_details = sum(
            int(item.run_metadata.get("youtube_details_reserved", 0)) for item in reservations
        )
        if (
            used_search + 1 > settings.youtube_discovery_daily_search_limit
            or used_details + 1 > settings.youtube_discovery_daily_hydration_limit
        ):
            raise DiscoveryQuotaExhausted("YouTube discovery daily budget exhausted")
        metadata = dict(run.run_metadata or {})
        current_search = (
            int(metadata.get("youtube_search_reserved", 0))
            if metadata.get("youtube_quota_day") == day
            else 0
        )
        current_details = (
            int(metadata.get("youtube_details_reserved", 0))
            if metadata.get("youtube_quota_day") == day
            else 0
        )
        metadata.update(
            {
                "youtube_quota_day": day,
                "youtube_search_reserved": current_search + 1,
                "youtube_details_reserved": current_details + 1,
            }
        )
        run.run_metadata = metadata
        estimate = {
            "day": day,
            "search_reserved": used_search + 1,
            "search_limit": settings.youtube_discovery_daily_search_limit,
            "details_reserved": used_details + 1,
            "details_limit": settings.youtube_discovery_daily_hydration_limit,
            "cost_basis": "one search and one possible video-details request per page",
        }
        session.add(
            DomainEvent(
                aggregate_type="discovery_run",
                aggregate_id=str(run_id),
                event_type="discovery_run.quota_reserved",
                payload={"discovery_run_id": str(run_id), **estimate},
            )
        )
        return estimate
