from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from katcha.acquisition.adapters import get_adapter
from katcha.acquisition_models import DiscoveryRun, IngestionSource
from katcha.db import session_scope
from katcha.domain import SourceUsageMode
from katcha.models import DomainEvent
from katcha.services.acquisition import register_discovery_run


def _clean_key(value: str, *, field: str) -> str:
    cleaned = value.strip().casefold()
    if not cleaned:
        raise ValueError(f"{field} must not be blank")
    return cleaned


def _validate_adapter(adapter_key: str, adapter_version: str) -> None:
    get_adapter(adapter_key, adapter_version)


def _source_run_metadata(
    source: IngestionSource,
    metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    default_candidate_metadata = {
        "ingestion_source_id": str(source.id),
        "ingestion_source_key": source.source_key,
        "source_platform": source.platform,
        "source_usage_mode": source.usage_mode,
        **dict(source.default_candidate_metadata or {}),
    }
    return {
        **dict(metadata or {}),
        "ingestion_source_id": str(source.id),
        "ingestion_source_key": source.source_key,
        "source_platform": source.platform,
        "source_usage_mode": source.usage_mode,
        "default_candidate_metadata": default_candidate_metadata,
    }


def upsert_ingestion_source(
    *,
    source_key: str,
    name: str,
    adapter_key: str,
    adapter_version: str,
    platform: str,
    usage_mode: SourceUsageMode = SourceUsageMode.CANDIDATE_REVIEW,
    query_template: dict[str, Any] | None = None,
    default_candidate_metadata: dict[str, Any] | None = None,
    source_metadata: dict[str, Any] | None = None,
    channel_profile_id: uuid.UUID | None = None,
    enabled: bool = True,
    poll_interval_minutes: int = 60,
) -> IngestionSource:
    source_key = _clean_key(source_key, field="source_key")
    name = name.strip()
    if not name:
        raise ValueError("name must not be blank")
    adapter_key = _clean_key(adapter_key, field="adapter_key")
    adapter_version = adapter_version.strip()
    if not adapter_version:
        raise ValueError("adapter_version must not be blank")
    platform = _clean_key(platform, field="platform")
    if poll_interval_minutes < 1:
        raise ValueError("poll_interval_minutes must be positive")
    _validate_adapter(adapter_key, adapter_version)

    with session_scope() as session:
        existing = session.scalar(
            select(IngestionSource).where(IngestionSource.source_key == source_key)
        )
        if existing is None:
            row = IngestionSource(
                source_key=source_key,
                name=name,
                channel_profile_id=channel_profile_id,
                adapter_key=adapter_key,
                adapter_version=adapter_version,
                platform=platform,
                usage_mode=usage_mode.value,
                query_template=dict(query_template or {}),
                default_candidate_metadata=dict(default_candidate_metadata or {}),
                source_metadata=dict(source_metadata or {}),
                enabled=enabled,
                poll_interval_minutes=poll_interval_minutes,
            )
            session.add(row)
            event_type = "ingestion_source.created"
        else:
            row = existing
            row.name = name
            row.channel_profile_id = channel_profile_id
            row.adapter_key = adapter_key
            row.adapter_version = adapter_version
            row.platform = platform
            row.usage_mode = usage_mode.value
            row.query_template = dict(query_template or {})
            row.default_candidate_metadata = dict(default_candidate_metadata or {})
            row.source_metadata = dict(source_metadata or {})
            row.enabled = enabled
            row.poll_interval_minutes = poll_interval_minutes
            event_type = "ingestion_source.updated"
        session.flush()
        session.add(
            DomainEvent(
                aggregate_type="ingestion_source",
                aggregate_id=str(row.id),
                event_type=event_type,
                payload={
                    "ingestion_source_id": str(row.id),
                    "source_key": row.source_key,
                    "adapter_key": row.adapter_key,
                    "adapter_version": row.adapter_version,
                    "platform": row.platform,
                    "usage_mode": row.usage_mode,
                    "enabled": row.enabled,
                },
            )
        )
        session.refresh(row)
        session.expunge(row)
        return row


def list_ingestion_sources(
    *,
    channel_profile_id: uuid.UUID | None = None,
    enabled: bool | None = None,
) -> list[IngestionSource]:
    with session_scope() as session:
        stmt = select(IngestionSource).order_by(
            IngestionSource.enabled.desc(),
            IngestionSource.source_key.asc(),
        )
        if channel_profile_id is not None:
            stmt = stmt.where(IngestionSource.channel_profile_id == channel_profile_id)
        if enabled is not None:
            stmt = stmt.where(IngestionSource.enabled == enabled)
        rows = list(session.scalars(stmt))
        for row in rows:
            session.expunge(row)
        return rows


def create_discovery_run_from_source(
    source_id: uuid.UUID,
    *,
    query_overrides: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> DiscoveryRun:
    with session_scope() as session:
        source = session.get(IngestionSource, source_id)
        if source is None:
            raise ValueError(f"ingestion source not found: {source_id}")
        if not source.enabled:
            raise ValueError("ingestion source is disabled")
        _validate_adapter(source.adapter_key, source.adapter_version)
        query = {
            **dict(source.query_template or {}),
            **dict(query_overrides or {}),
        }
        run_metadata = _source_run_metadata(source, metadata)
        adapter_key = source.adapter_key
        adapter_version = source.adapter_version

    return register_discovery_run(
        adapter_key=adapter_key,
        adapter_version=adapter_version,
        query=query,
        idempotency_key=idempotency_key,
        metadata=run_metadata,
    )
