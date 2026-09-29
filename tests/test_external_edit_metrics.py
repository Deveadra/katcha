import uuid
from contextlib import contextmanager
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from katcha.api.integrations import InVideoMetricsUpdate
from katcha.external_edit_models import ExternalEditHandoff
from katcha.models import DomainEvent, UsageEvent
from katcha.services import external_edit


@pytest.fixture
def metrics_db(monkeypatch):
    engine = create_engine("sqlite://")
    for model in (ExternalEditHandoff, DomainEvent, UsageEvent):
        model.__table__.create(engine)

    @contextmanager
    def scope():
        with Session(engine, expire_on_commit=False) as session, session.begin():
            yield session

    monkeypatch.setattr(external_edit, "session_scope", scope)
    handoff_id = uuid.uuid4()
    with scope() as session:
        session.add(ExternalEditHandoff(
            id=handoff_id, provider="invideo", source_type="short_episode",
            source_id=uuid.uuid4(), status="prepared", package_manifest_key="fixture",
            handoff_metadata={"verification": {"verified": True}},
        ))
    yield handoff_id, scope
    engine.dispose()


def test_metrics_retries_and_corrections_preserve_total_and_history(metrics_db):
    handoff_id, scope = metrics_db
    record = external_edit.record_external_edit_metrics
    record(handoff_id, cost_usd=2.8, credits_used=42)
    first = record(handoff_id, cost_usd=2.8, credits_used=42)
    record(handoff_id, production_minutes=18)
    record(handoff_id, cost_usd=1.2)
    row = record(handoff_id, cost_usd=0)
    assert row.handoff_metadata["verification"] == {"verified": True}
    assert row.handoff_metadata["provider_metrics"]["credits_used"] == 42
    assert row.handoff_metadata["provider_metrics"]["production_minutes"] == 18
    with scope() as session:
        assert session.scalar(select(func.sum(UsageEvent.cost_usd))) == Decimal("0")
        assert session.scalar(select(func.count()).select_from(UsageEvent)) == 3
        assert session.scalar(select(func.count()).select_from(DomainEvent)) == 4
        events = list(session.scalars(select(DomainEvent)))
        assert events[0].payload["provider_metrics"]["recorded_at"] == first.handoff_metadata[
            "provider_metrics"
        ]["recorded_at"]


@pytest.mark.parametrize("values", [
    {}, {"cost_usd": -1}, {"cost_usd": float("nan")},
    {"credits_used": float("inf")}, {"production_minutes": -1},
    {"manual_interventions": 1.5}, {"manual_interventions": True},
    {"cost_usd": 1_000_000},
])
def test_invalid_metrics_do_not_write(metrics_db, values):
    handoff_id, scope = metrics_db
    with pytest.raises(ValueError):
        external_edit.record_external_edit_metrics(handoff_id, **values)
    with scope() as session:
        assert session.scalar(select(func.count()).select_from(DomainEvent)) == 0
        assert session.scalar(select(func.count()).select_from(UsageEvent)) == 0


def test_missing_and_cancelled_handoffs(metrics_db):
    handoff_id, scope = metrics_db
    with pytest.raises(ValueError, match="not found"):
        external_edit.record_external_edit_metrics(uuid.uuid4(), cost_usd=1)
    with scope() as session:
        session.get(ExternalEditHandoff, handoff_id).status = "cancelled"
    with pytest.raises(ValueError, match="cancelled"):
        external_edit.record_external_edit_metrics(handoff_id, cost_usd=1)


@pytest.mark.parametrize("values", [
    {"cost_usd": float("inf")}, {"credits_used": float("nan")},
    {"production_minutes": float("inf")}, {"manual_interventions": 1.5},
    {"cost_usd": 1_000_000},
])
def test_api_rejects_invalid_metrics(values):
    with pytest.raises(ValidationError):
        InVideoMetricsUpdate(**values)
