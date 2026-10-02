from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_, select

from katcha.acquisition.adapters import get_adapter
from katcha.acquisition_models import (
    DiscoveryCandidate,
    DiscoveryObservation,
    DiscoveryRun,
    IngestionSource,
    IntelligenceBatchRecord,
    IntelligenceIngestBatch,
    IntelligenceRecord,
)
from katcha.db import session_scope
from katcha.domain import DiscoveryRunStatus, SourceUsageMode
from katcha.intelligence_models import ChannelProfile
from katcha.models import DomainEvent
from katcha.services.acquisition import register_discovery_run


@dataclass(frozen=True, slots=True)
class SourceLibraryPage:
    items: list[IngestionSource]
    total: int
    limit: int
    offset: int


@dataclass(frozen=True, slots=True)
class SourceRecentFind:
    candidate: DiscoveryCandidate
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class SourceFindPage:
    items: list[SourceRecentFind]
    total: int
    limit: int
    offset: int


@dataclass(frozen=True, slots=True)
class SourceOverview:
    source: IngestionSource
    channel_name: str | None
    channel_status: str | None
    run_count: int
    status_counts: dict[str, int]
    discovery_count: int
    unique_candidate_count: int
    recent_runs: list[DiscoveryRun]
    recent_finds: list[SourceRecentFind]


@dataclass(frozen=True, slots=True)
class SourceImportRun:
    discovery_run: DiscoveryRun
    batch_key: str | None
    item_count: int


@dataclass(frozen=True, slots=True)
class IntelligenceIngestResult:
    batch: IntelligenceIngestBatch
    records: list[IntelligenceRecord]
    created_count: int
    updated_count: int
    replayed: bool


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
    source_scope = "channel" if source.channel_profile_id is not None else "shared"
    default_candidate_metadata = {
        **dict(source.default_candidate_metadata or {}),
        "ingestion_source_id": str(source.id),
        "ingestion_source_key": source.source_key,
        "source_platform": source.platform,
        "source_usage_mode": source.usage_mode,
        "source_scope": source_scope,
        "channel_profile_id": (
            str(source.channel_profile_id) if source.channel_profile_id is not None else None
        ),
    }
    return {
        **dict(metadata or {}),
        "ingestion_source_id": str(source.id),
        "ingestion_source_key": source.source_key,
        "source_platform": source.platform,
        "source_usage_mode": source.usage_mode,
        "source_scope": source_scope,
        "channel_profile_id": (
            str(source.channel_profile_id)
            if source.channel_profile_id is not None
            else None
        ),
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
    create_only: bool = False,
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
        if existing is not None and create_only:
            raise ValueError("source key already exists")
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


def list_ingestion_source_library(
    *,
    query: str | None = None,
    channel_profile_id: uuid.UUID | None = None,
    shared_only: bool = False,
    platform: str | None = None,
    adapter_key: str | None = None,
    usage_mode: str | None = None,
    enabled: bool | None = None,
    sort: str = "recent",
    limit: int = 50,
    offset: int = 0,
) -> SourceLibraryPage:
    cleaned_query = (query or "").strip().casefold()
    cleaned_platform = (platform or "").strip().casefold()
    cleaned_adapter = (adapter_key or "").strip().casefold()
    cleaned_usage = (usage_mode or "").strip()
    if limit < 1 or limit > 200:
        raise ValueError("limit must be between 1 and 200")
    if offset < 0:
        raise ValueError("offset must be non-negative")
    if shared_only and channel_profile_id is not None:
        raise ValueError("shared_only and channel_profile_id cannot be combined")
    if sort not in {"recent", "name", "created"}:
        raise ValueError("unsupported source-library sort")

    filters = []
    if cleaned_query:
        pattern = f"%{cleaned_query}%"
        filters.append(
            or_(
                func.lower(IngestionSource.name).like(pattern),
                func.lower(IngestionSource.source_key).like(pattern),
            )
        )
    if channel_profile_id is not None:
        filters.append(IngestionSource.channel_profile_id == channel_profile_id)
    elif shared_only:
        filters.append(IngestionSource.channel_profile_id.is_(None))
    if cleaned_platform:
        filters.append(IngestionSource.platform == cleaned_platform)
    if cleaned_adapter:
        filters.append(IngestionSource.adapter_key == cleaned_adapter)
    if cleaned_usage:
        filters.append(IngestionSource.usage_mode == cleaned_usage)
    if enabled is not None:
        filters.append(IngestionSource.enabled == enabled)

    order = (
        (IngestionSource.updated_at.desc(), IngestionSource.id.desc())
        if sort == "recent"
        else (
            (func.lower(IngestionSource.name).asc(), IngestionSource.id.asc())
            if sort == "name"
            else (IngestionSource.created_at.desc(), IngestionSource.id.desc())
        )
    )

    with session_scope() as session:
        total = int(
            session.scalar(
                select(func.count())
                .select_from(IngestionSource)
                .where(*filters)
            )
            or 0
        )
        rows = list(
            session.scalars(
                select(IngestionSource)
                .where(*filters)
                .order_by(*order)
                .offset(offset)
                .limit(limit)
            )
        )
        for row in rows:
            session.expunge(row)
        return SourceLibraryPage(
            items=rows,
            total=total,
            limit=limit,
            offset=offset,
        )


def list_source_finds(
    source_id: uuid.UUID,
    *,
    query: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> SourceFindPage:
    cleaned_query = (query or "").strip().casefold()
    cleaned_status = (status or "").strip().casefold()
    if limit < 1 or limit > 200:
        raise ValueError("limit must be between 1 and 200")
    if offset < 0:
        raise ValueError("offset must be non-negative")

    with session_scope() as session:
        if session.get(IngestionSource, source_id) is None:
            raise ValueError("ingestion source not found")

        source_filter = (
            DiscoveryRun.run_metadata["ingestion_source_id"].as_string()
            == str(source_id)
        )
        latest_observation = (
            select(
                DiscoveryObservation.discovery_candidate_id.label("candidate_id"),
                func.max(DiscoveryObservation.observed_at).label("observed_at"),
            )
            .join(
                DiscoveryRun,
                DiscoveryObservation.discovery_run_id == DiscoveryRun.id,
            )
            .where(source_filter)
            .group_by(DiscoveryObservation.discovery_candidate_id)
            .subquery()
        )

        filters = []
        if cleaned_query:
            pattern = f"%{cleaned_query}%"
            filters.append(
                or_(
                    func.lower(func.coalesce(DiscoveryCandidate.title, "")).like(pattern),
                    func.lower(func.coalesce(DiscoveryCandidate.creator, "")).like(pattern),
                    func.lower(DiscoveryCandidate.source_url).like(pattern),
                )
            )
        if cleaned_status:
            filters.append(DiscoveryCandidate.status == cleaned_status)

        total = int(
            session.scalar(
                select(func.count())
                .select_from(DiscoveryCandidate)
                .join(
                    latest_observation,
                    latest_observation.c.candidate_id == DiscoveryCandidate.id,
                )
                .where(*filters)
            )
            or 0
        )
        rows = session.execute(
            select(
                DiscoveryCandidate,
                latest_observation.c.observed_at,
            )
            .join(
                latest_observation,
                latest_observation.c.candidate_id == DiscoveryCandidate.id,
            )
            .where(*filters)
            .order_by(
                latest_observation.c.observed_at.desc(),
                DiscoveryCandidate.id.desc(),
            )
            .offset(offset)
            .limit(limit)
        ).all()

        items = [
            SourceRecentFind(candidate=candidate, observed_at=observed_at)
            for candidate, observed_at in rows
        ]
        for item in items:
            session.expunge(item.candidate)
        return SourceFindPage(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
        )


def get_ingestion_source_overview(
    source_id: uuid.UUID,
    *,
    recent_run_limit: int = 8,
    recent_find_limit: int = 8,
) -> SourceOverview:
    if recent_run_limit < 1 or recent_run_limit > 50:
        raise ValueError("recent_run_limit must be between 1 and 50")
    if recent_find_limit < 1 or recent_find_limit > 50:
        raise ValueError("recent_find_limit must be between 1 and 50")

    with session_scope() as session:
        source = session.get(IngestionSource, source_id)
        if source is None:
            raise ValueError("ingestion source not found")

        source_filter = (
            DiscoveryRun.run_metadata["ingestion_source_id"].as_string()
            == str(source_id)
        )
        status_rows = session.execute(
            select(DiscoveryRun.status, func.count())
            .where(source_filter)
            .group_by(DiscoveryRun.status)
        ).all()
        status_counts = {str(status): int(count) for status, count in status_rows}
        run_count = sum(status_counts.values())

        recent_runs = list(
            session.scalars(
                select(DiscoveryRun)
                .where(source_filter)
                .order_by(DiscoveryRun.created_at.desc(), DiscoveryRun.id.desc())
                .limit(recent_run_limit)
            )
        )

        discovery_count, unique_candidate_count = session.execute(
            select(
                func.count(DiscoveryObservation.id),
                func.count(func.distinct(DiscoveryObservation.discovery_candidate_id)),
            )
            .select_from(DiscoveryObservation)
            .join(
                DiscoveryRun,
                DiscoveryObservation.discovery_run_id == DiscoveryRun.id,
            )
            .where(source_filter)
        ).one()

        recent_rows = session.execute(
            select(DiscoveryCandidate, DiscoveryObservation.observed_at)
            .join(
                DiscoveryObservation,
                DiscoveryObservation.discovery_candidate_id == DiscoveryCandidate.id,
            )
            .join(
                DiscoveryRun,
                DiscoveryObservation.discovery_run_id == DiscoveryRun.id,
            )
            .where(source_filter)
            .order_by(DiscoveryObservation.observed_at.desc())
            .limit(max(recent_find_limit * 4, recent_find_limit))
        ).all()

        seen: set[uuid.UUID] = set()
        recent_finds: list[SourceRecentFind] = []
        for candidate, observed_at in recent_rows:
            if candidate.id in seen:
                continue
            seen.add(candidate.id)
            recent_finds.append(
                SourceRecentFind(
                    candidate=candidate,
                    observed_at=observed_at,
                )
            )
            if len(recent_finds) >= recent_find_limit:
                break

        channel_name: str | None = None
        channel_status: str | None = None
        if source.channel_profile_id is not None:
            profile = session.get(ChannelProfile, source.channel_profile_id)
            if profile is not None:
                metadata = dict(profile.profile_metadata or {})
                channel_name = str(
                    metadata.get("channel_title")
                    or metadata.get("name")
                    or metadata.get("channel_handle")
                    or profile.id
                )
                channel_status = str(profile.status or "") or None

        session.expunge(source)
        for row in recent_runs:
            session.expunge(row)
        for item in recent_finds:
            session.expunge(item.candidate)

        return SourceOverview(
            source=source,
            channel_name=channel_name,
            channel_status=channel_status,
            run_count=run_count,
            status_counts=status_counts,
            discovery_count=int(discovery_count or 0),
            unique_candidate_count=int(unique_candidate_count or 0),
            recent_runs=recent_runs,
            recent_finds=recent_finds,
        )


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
        if source.usage_mode == SourceUsageMode.BLOCKED.value:
            raise ValueError("ingestion source is blocked")
        _validate_adapter(source.adapter_key, source.adapter_version)
        query = {
            **dict(source.query_template or {}),
            **dict(query_overrides or {}),
        }
        if (
            source.adapter_key == "youtube"
            and source.channel_profile_id is not None
            and not query.get("youtube_connection_id")
        ):
            profile = session.get(ChannelProfile, source.channel_profile_id)
            if profile is not None and profile.youtube_connection_id is not None:
                query["youtube_connection_id"] = str(profile.youtube_connection_id)
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
        "urls": cleaned_urls,
        "items": cleaned_items,
    }
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


def list_source_runs(source_id: uuid.UUID, *, limit: int = 50) -> list[DiscoveryRun]:
    """Return bounded history using the source identity frozen on each run."""
    with session_scope() as session:
        if session.get(IngestionSource, source_id) is None:
            raise ValueError("ingestion source not found")
        rows = list(session.scalars(
            select(DiscoveryRun)
            .where(DiscoveryRun.run_metadata["ingestion_source_id"].as_string() == str(source_id))
            .order_by(DiscoveryRun.created_at.desc(), DiscoveryRun.id.desc())
            .limit(limit)
        ))
        for row in rows:
            session.expunge(row)
        return rows

def restart_source_run(
    run_id: uuid.UUID,
    *,
    idempotency_key: str | None = None,
) -> DiscoveryRun:
    with session_scope() as session:
        run = session.get(DiscoveryRun, run_id)
        if run is None:
            raise ValueError("discovery run not found")
        if run.status != DiscoveryRunStatus.FAILED.value:
            raise ValueError("only failed discovery runs can be restarted")
        metadata = dict(run.run_metadata or {})
        raw_source_id = metadata.get("ingestion_source_id")
        if not raw_source_id:
            raise ValueError("discovery run is not attached to an ingestion source")
        try:
            source_id = uuid.UUID(str(raw_source_id))
        except ValueError as exc:
            raise ValueError("discovery run has an invalid ingestion source ID") from exc
        source = session.get(IngestionSource, source_id)
        if source is None:
            raise ValueError("ingestion source not found")
        if not source.enabled:
            raise ValueError("ingestion source is disabled")
        if source.usage_mode == SourceUsageMode.BLOCKED.value:
            raise ValueError("ingestion source is blocked")
        adapter_key = run.adapter_key
        adapter_version = run.adapter_version
        query = dict(run.query or {})
        metadata.update(
            {
                "restarted_from_run_id": str(run.id),
                "recovery_mode": "manual_restart",
            }
        )

    return register_discovery_run(
        adapter_key=adapter_key,
        adapter_version=adapter_version,
        query=query,
        idempotency_key=idempotency_key,
        metadata=metadata,
    )


def list_resumable_source_runs(*, limit: int = 500) -> list[DiscoveryRun]:
    if limit < 1 or limit > 5000:
        raise ValueError("limit must be between 1 and 5000")
    with session_scope() as session:
        rows = list(
            session.scalars(
                select(DiscoveryRun)
                .where(
                    DiscoveryRun.status.in_(
                        [
                            DiscoveryRunStatus.QUEUED.value,
                            DiscoveryRunStatus.RUNNING.value,
                        ]
                    )
                )
                .order_by(DiscoveryRun.created_at.asc(), DiscoveryRun.id.asc())
                .limit(limit)
            )
        )
        resumable: list[DiscoveryRun] = []
        for run in rows:
            metadata = dict(run.run_metadata or {})
            raw_source_id = metadata.get("ingestion_source_id")
            if not raw_source_id:
                continue
            try:
                source_id = uuid.UUID(str(raw_source_id))
            except ValueError:
                continue
            source = session.get(IngestionSource, source_id)
            if (
                source is None
                or not source.enabled
                or source.usage_mode == SourceUsageMode.BLOCKED.value
            ):
                continue
            session.expunge(run)
            resumable.append(run)
        return resumable


def _clean_intelligence_slug(value: str, *, field: str, max_length: int) -> str:
    cleaned = value.strip().casefold()
    if not cleaned:
        raise ValueError(f"{field} must not be blank")
    if len(cleaned) > max_length:
        raise ValueError(f"{field} must be at most {max_length} characters")
    if any(not (char.isalnum() or char in {"_", "-", "."}) for char in cleaned):
        raise ValueError(
            f"{field} may contain only letters, numbers, underscore, dash, or dot"
        )
    return cleaned


def _clean_intelligence_key(value: str, *, field: str, max_length: int) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field} must not be blank")
    if len(cleaned) > max_length:
        raise ValueError(f"{field} must be at most {max_length} characters")
    return cleaned


def _clean_intelligence_text(value: object | None) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _coerce_intelligence_time(value: object | None, *, field: str) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        candidate = value.strip()
        if not candidate:
            return None
        if candidate.endswith("Z"):
            candidate = candidate[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError as exc:
            raise ValueError(f"{field} must be an ISO-8601 datetime") from exc
    else:
        raise ValueError(f"{field} must be a datetime or ISO-8601 string")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _normalize_intelligence_record(
    item: dict[str, Any],
    *,
    index: int,
) -> dict[str, Any]:
    record_kind = _clean_intelligence_slug(
        str(item.get("record_kind") or ""),
        field=f"records[{index}].record_kind",
        max_length=64,
    )
    record_key = _clean_intelligence_key(
        str(item.get("record_key") or ""),
        field=f"records[{index}].record_key",
        max_length=255,
    )
    raw_tags = item.get("tags") or []
    if not isinstance(raw_tags, list):
        raise ValueError(f"records[{index}].tags must be a list")
    tags = sorted(
        {
            _clean_intelligence_slug(
                str(tag),
                field=f"records[{index}].tags",
                max_length=64,
            )
            for tag in raw_tags
        }
    )
    payload = item.get("payload") or {}
    provenance = item.get("provenance") or {}
    if not isinstance(payload, dict):
        raise ValueError(f"records[{index}].payload must be an object")
    if not isinstance(provenance, dict):
        raise ValueError(f"records[{index}].provenance must be an object")
    platform_value = _clean_intelligence_text(item.get("platform"))
    platform = (
        _clean_intelligence_slug(
            platform_value,
            field=f"records[{index}].platform",
            max_length=32,
        )
        if platform_value is not None
        else None
    )
    status_value = _clean_intelligence_text(item.get("status")) or "active"
    status = _clean_intelligence_slug(
        status_value,
        field=f"records[{index}].status",
        max_length=32,
    )
    return {
        "record_kind": record_kind,
        "record_key": record_key,
        "title": _clean_intelligence_text(item.get("title")),
        "summary": _clean_intelligence_text(item.get("summary")),
        "source_url": _clean_intelligence_text(item.get("source_url")),
        "platform": platform,
        "status": status,
        "tags": tags,
        "payload": dict(payload),
        "provenance": dict(provenance),
        "observed_at": _coerce_intelligence_time(
            item.get("observed_at"),
            field=f"records[{index}].observed_at",
        ),
        "event_time": _coerce_intelligence_time(
            item.get("event_time"),
            field=f"records[{index}].event_time",
        ),
    }


def _json_fingerprint_value(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"unsupported fingerprint value: {type(value).__name__}")


def _intelligence_batch_fingerprint(
    *,
    channel_profile_id: uuid.UUID,
    batch_key: str,
    producer: str,
    source_type: str,
    batch_metadata: dict[str, Any],
    records: list[dict[str, Any]],
) -> str:
    payload = {
        "channel_profile_id": str(channel_profile_id),
        "batch_key": batch_key,
        "producer": producer,
        "source_type": source_type,
        "batch_metadata": batch_metadata,
        "records": records,
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_json_fingerprint_value,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _detach_intelligence_records(session, rows: list[IntelligenceRecord]) -> None:
    for row in rows:
        session.expunge(row)


def _replay_intelligence_batch(
    session,
    batch: IntelligenceIngestBatch,
) -> IntelligenceIngestResult:
    memberships = list(
        session.scalars(
            select(IntelligenceBatchRecord)
            .where(IntelligenceBatchRecord.batch_id == batch.id)
            .order_by(IntelligenceBatchRecord.ordinal.asc())
        )
    )
    records: list[IntelligenceRecord] = []
    for membership in memberships:
        row = session.get(IntelligenceRecord, membership.record_id)
        if row is not None:
            records.append(row)
    created_count = sum(1 for item in memberships if item.action == "created")
    updated_count = sum(1 for item in memberships if item.action == "updated")
    _detach_intelligence_records(session, records)
    session.expunge(batch)
    return IntelligenceIngestResult(
        batch=batch,
        records=records,
        created_count=created_count,
        updated_count=updated_count,
        replayed=True,
    )


def ingest_intelligence_batch(
    *,
    channel_profile_id: uuid.UUID,
    batch_key: str,
    producer: str,
    source_type: str,
    records: list[dict[str, Any]],
    batch_metadata: dict[str, Any] | None = None,
) -> IntelligenceIngestResult:
    if not records:
        raise ValueError("intelligence batch must contain at least one record")
    if len(records) > 500:
        raise ValueError("intelligence batch may contain at most 500 records")
    cleaned_batch_key = _clean_intelligence_key(
        batch_key,
        field="batch_key",
        max_length=160,
    )
    cleaned_producer = _clean_intelligence_key(
        producer,
        field="producer",
        max_length=128,
    )
    cleaned_source_type = _clean_intelligence_slug(
        source_type,
        field="source_type",
        max_length=64,
    )
    cleaned_batch_metadata = dict(batch_metadata or {})
    normalized_records = [
        _normalize_intelligence_record(item, index=index)
        for index, item in enumerate(records)
    ]
    identities = [
        (item["record_kind"], item["record_key"]) for item in normalized_records
    ]
    if len(identities) != len(set(identities)):
        raise ValueError("intelligence batch contains duplicate record identities")
    fingerprint = _intelligence_batch_fingerprint(
        channel_profile_id=channel_profile_id,
        batch_key=cleaned_batch_key,
        producer=cleaned_producer,
        source_type=cleaned_source_type,
        batch_metadata=cleaned_batch_metadata,
        records=normalized_records,
    )

    with session_scope() as session:
        if session.get(ChannelProfile, channel_profile_id) is None:
            raise ValueError(f"channel profile not found: {channel_profile_id}")
        existing_batch = session.scalar(
            select(IntelligenceIngestBatch).where(
                IntelligenceIngestBatch.channel_profile_id == channel_profile_id,
                IntelligenceIngestBatch.batch_key == cleaned_batch_key,
            )
        )
        if existing_batch is not None:
            if existing_batch.content_sha256 != fingerprint:
                raise ValueError(
                    "batch_key already exists with different intelligence content"
                )
            return _replay_intelligence_batch(session, existing_batch)

        batch = IntelligenceIngestBatch(
            channel_profile_id=channel_profile_id,
            batch_key=cleaned_batch_key,
            producer=cleaned_producer,
            source_type=cleaned_source_type,
            content_sha256=fingerprint,
            record_count=len(normalized_records),
            batch_metadata=cleaned_batch_metadata,
        )
        session.add(batch)
        session.flush()

        stored_records: list[IntelligenceRecord] = []
        created_count = 0
        updated_count = 0
        now = datetime.now(UTC)
        for ordinal, item in enumerate(normalized_records):
            row = session.scalar(
                select(IntelligenceRecord).where(
                    IntelligenceRecord.channel_profile_id == channel_profile_id,
                    IntelligenceRecord.record_kind == item["record_kind"],
                    IntelligenceRecord.record_key == item["record_key"],
                )
            )
            observed_at = item["observed_at"] or now
            if row is None:
                row = IntelligenceRecord(
                    channel_profile_id=channel_profile_id,
                    first_batch_id=batch.id,
                    last_batch_id=batch.id,
                    record_kind=item["record_kind"],
                    record_key=item["record_key"],
                    title=item["title"],
                    summary=item["summary"],
                    source_url=item["source_url"],
                    platform=item["platform"],
                    status=item["status"],
                    tags=item["tags"],
                    payload=item["payload"],
                    provenance=item["provenance"],
                    observed_at=observed_at,
                    event_time=item["event_time"],
                )
                session.add(row)
                action = "created"
                created_count += 1
            else:
                row.last_batch_id = batch.id
                row.title = item["title"]
                row.summary = item["summary"]
                row.source_url = item["source_url"]
                row.platform = item["platform"]
                row.status = item["status"]
                row.tags = item["tags"]
                row.payload = item["payload"]
                row.provenance = item["provenance"]
                row.observed_at = observed_at
                row.event_time = item["event_time"]
                action = "updated"
                updated_count += 1
            session.flush()
            session.add(
                IntelligenceBatchRecord(
                    batch_id=batch.id,
                    record_id=row.id,
                    ordinal=ordinal,
                    action=action,
                )
            )
            stored_records.append(row)

        session.add(
            DomainEvent(
                aggregate_type="intelligence_ingest_batch",
                aggregate_id=str(batch.id),
                event_type="intelligence_ingest.batch_committed",
                payload={
                    "channel_profile_id": str(channel_profile_id),
                    "batch_key": cleaned_batch_key,
                    "producer": cleaned_producer,
                    "source_type": cleaned_source_type,
                    "record_count": len(stored_records),
                    "created_count": created_count,
                    "updated_count": updated_count,
                    "content_sha256": fingerprint,
                },
            )
        )
        session.flush()
        session.refresh(batch)
        for row in stored_records:
            session.refresh(row)
        _detach_intelligence_records(session, stored_records)
        session.expunge(batch)
        return IntelligenceIngestResult(
            batch=batch,
            records=stored_records,
            created_count=created_count,
            updated_count=updated_count,
            replayed=False,
        )


def list_intelligence_records(
    channel_profile_id: uuid.UUID,
    *,
    record_kind: str | None = None,
    record_status: str | None = None,
    limit: int = 100,
) -> list[IntelligenceRecord]:
    if limit < 1 or limit > 500:
        raise ValueError("limit must be between 1 and 500")
    cleaned_kind = (
        _clean_intelligence_slug(record_kind, field="record_kind", max_length=64)
        if record_kind is not None
        else None
    )
    cleaned_status = (
        _clean_intelligence_slug(record_status, field="status", max_length=32)
        if record_status is not None
        else None
    )
    with session_scope() as session:
        if session.get(ChannelProfile, channel_profile_id) is None:
            raise ValueError(f"channel profile not found: {channel_profile_id}")
        stmt = (
            select(IntelligenceRecord)
            .where(IntelligenceRecord.channel_profile_id == channel_profile_id)
            .order_by(
                IntelligenceRecord.observed_at.desc(),
                IntelligenceRecord.updated_at.desc(),
                IntelligenceRecord.id.desc(),
            )
            .limit(limit)
        )
        if cleaned_kind is not None:
            stmt = stmt.where(IntelligenceRecord.record_kind == cleaned_kind)
        if cleaned_status is not None:
            stmt = stmt.where(IntelligenceRecord.status == cleaned_status)
        rows = list(session.scalars(stmt))
        _detach_intelligence_records(session, rows)
        return rows


def get_intelligence_record(
    channel_profile_id: uuid.UUID,
    record_id: uuid.UUID,
) -> IntelligenceRecord:
    with session_scope() as session:
        row = session.get(IntelligenceRecord, record_id)
        if row is None or row.channel_profile_id != channel_profile_id:
            raise ValueError(f"intelligence record not found: {record_id}")
        session.expunge(row)
        return row
