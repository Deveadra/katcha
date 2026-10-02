"""Opt-in recurring discovery. A paused source never creates new checks."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from katcha.acquisition_models import IngestionSource
from katcha.config import get_settings
from katcha.db import session_scope
from katcha.models import DomainEvent
from katcha.services.research import PULL_ADAPTERS


def configure_polling(
    source_id: uuid.UUID, *, enabled: bool, interval_minutes: int, actor: str
) -> dict:
    if not 15 <= interval_minutes <= 10080:
        raise ValueError("Choose a check interval between 15 minutes and 7 days")
    with session_scope() as session:
        row = session.scalar(
            select(IngestionSource).where(IngestionSource.id == source_id).with_for_update()
        )
        if row is None:
            raise ValueError("Source not found")
        if row.adapter_key not in PULL_ADAPTERS:
            raise ValueError("This source accepts manual imports and cannot run recurring searches")
        if enabled and (not row.enabled or row.usage_mode == "blocked"):
            raise ValueError("Resume the source before enabling recurring checks")
        if enabled and not get_settings().research_enabled:
            raise ValueError(
                "Automatic research is disabled in runtime settings. "
                "Enable it before scheduling checks"
            )
        config = {
            "enabled": enabled,
            "interval_minutes": interval_minutes,
            "workflow_id": "katcha-automatic-research",
        }
        row.source_metadata = {
            **dict(row.source_metadata or {}),
            "automatic_research": enabled,
            "execution_mode": "recurring" if enabled else "manual",
        }
        row.poll_interval_minutes = interval_minutes
        session.add(
            DomainEvent(
                aggregate_type="ingestion_source",
                aggregate_id=str(source_id),
                event_type="source.polling_configured",
                payload={
                    "actor": actor,
                    "channel_profile_id": str(row.channel_profile_id),
                    **config,
                },
            )
        )
        return config
