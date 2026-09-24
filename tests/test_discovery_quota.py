from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import katcha.db as db
import katcha.services.discovery_quota as quota
from katcha.acquisition_models import DiscoveryRun
from katcha.config import Settings
from katcha.models import DomainEvent


def test_budget_counts_every_attempt_and_resets_on_pacific_day(monkeypatch):
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    db.Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", factory)
    monkeypatch.setattr(
        quota,
        "get_settings",
        lambda: Settings(
            youtube_discovery_daily_search_limit=2,
            youtube_discovery_daily_hydration_limit=2,
        ),
    )
    first, second = uuid.uuid4(), uuid.uuid4()
    with factory.begin() as session:
        for ident in [first, second]:
            session.add(
                DiscoveryRun(
                    id=ident,
                    adapter_key="youtube",
                    adapter_version="v1",
                    run_key=str(ident),
                    query={},
                    cursor={},
                    run_metadata={},
                )
            )
    before = datetime(2026, 9, 25, 6, 59, tzinfo=UTC)  # 23:59 PDT
    after = before + timedelta(minutes=2)
    assert quota.reserve_youtube_page(first, now=before)["search_reserved"] == 1
    assert quota.reserve_youtube_page(second, now=before)["search_reserved"] == 2
    with pytest.raises(quota.DiscoveryQuotaExhausted):
        quota.reserve_youtube_page(first, now=before)
    assert quota.reserve_youtube_page(first, now=after)["search_reserved"] == 1
    with factory() as session:
        assert session.get(DiscoveryRun, first).run_metadata["youtube_search_reserved"] == 1
        events = list(
            session.scalars(
                select(DomainEvent).where(DomainEvent.event_type == "discovery_run.quota_reserved")
            )
        )
        assert len(events) == 3
        assert all("api_key" not in event.payload for event in events)
    engine.dispose()
