from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from katcha.acquisition.adapters import get_adapter
from katcha.acquisition_models import DiscoveryRun, IngestionSource
from katcha.db import session_scope
from katcha.domain import SourceUsageMode
from katcha.models import DomainEvent
from katcha.services.acquisition import register_discovery_run


@dataclass(frozen=True, slots=True)
class SourceImportRun:
    discovery_run: DiscoveryRun
    batch_key: str | None
    item_count: int


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


def _clean_optional(value: str | None, *, field: str) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field} must not be blank")
    return cleaned


def _clean_urls(urls: list[str] | None) -> list[str]:
    cleaned: list[str] = []
    for index, value in enumerate(urls or []):
        url = _clean_optional(str(value), field=f"urls[{index}]")
        if url is not None:
            cleaned.append(url)
    return cleaned


def _clean_items(items: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [dict(item) for item in (items or [])]


def create_source_import_run(
    source_id: uuid.UUID,
    *,
    urls: list[str] | None = None,
    items: list[dict[str, Any]] | None = None,
    batch_key: str | None = None,
    idempotency_key: str | None = None,
    feed_key: str | None = None,
    default_platform: str | None = None,
    default_content_kind: str | None = None,
    default_metadata: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> SourceImportRun:
    cleaned_urls = _clean_urls(urls)
    cleaned_items = _clean_items(items)
    item_count = len(cleaned_urls) + len(cleaned_items)
    if item_count < 1:
        raise ValueError("source import must include at least one url or item")
    cleaned_batch_key = _clean_optional(batch_key, field="batch_key")
    cleaned_feed_key = _clean_optional(feed_key, field="feed_key")
    cleaned_default_platform = _clean_optional(
        default_platform,
        field="default_platform",
    )
    cleaned_default_content_kind = _clean_optional(
        default_content_kind,
        field="default_content_kind",
    )

    with session_scope() as session:
        source = session.get(IngestionSource, source_id)
        if source is None:
            raise ValueError(f"ingestion source not found: {source_id}")
        if source.adapter_key != "operator_feed":
            raise ValueError("source imports require operator_feed@v1")
        if source.adapter_version != "v1":
            raise ValueError("source imports require operator_feed@v1")
        template = dict(source.query_template or {})
        source_key = source.source_key

    template_default_metadata = dict(template.get("default_metadata") or {})
    import_default_metadata = {
        **template_default_metadata,
        **dict(default_metadata or {}),
        "source_import_item_count": item_count,
    }
    if cleaned_batch_key is not None:
        import_default_metadata["source_import_batch_key"] = cleaned_batch_key

    query_overrides: dict[str, Any] = {
        "default_metadata": import_default_metadata,
    }
    if cleaned_urls:
        query_overrides["urls"] = cleaned_urls
    if cleaned_items:
        query_overrides["items"] = cleaned_items
    if cleaned_feed_key is not None:
        query_overrides["feed_key"] = cleaned_feed_key
    if cleaned_default_platform is not None:
        query_overrides["default_platform"] = cleaned_default_platform
    if cleaned_default_content_kind is not None:
        query_overrides["default_content_kind"] = cleaned_default_content_kind

    run_metadata = {
        **dict(metadata or {}),
        "source_import_item_count": item_count,
    }
    if cleaned_batch_key is not None:
        run_metadata["source_import_batch_key"] = cleaned_batch_key
    resolved_idempotency_key = idempotency_key
    if not resolved_idempotency_key and cleaned_batch_key is not None:
        resolved_idempotency_key = f"source-import:{source_key}:{cleaned_batch_key}"

    return SourceImportRun(
        discovery_run=create_discovery_run_from_source(
            source_id,
            query_overrides=query_overrides,
            idempotency_key=resolved_idempotency_key,
            metadata=run_metadata,
        ),
        batch_key=cleaned_batch_key,
        item_count=item_count,
    )
