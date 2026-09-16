from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import and_, or_, select

from katcha.db import session_scope
from katcha.intelligence_models import EventConsumerCursor
from katcha.models import DomainEvent

_SENSITIVE_PARTS = (
    "token",
    "secret",
    "credential",
    "encrypted",
    "upload_url",
    "code_verifier",
)
_SCOPE_KEY = "_channel_profile_id"
_SCAN_BATCH = 5000
_MAX_SCAN_BATCHES = 20


def _safe_payload(value: object) -> object:
    if isinstance(value, dict):
        result: dict[str, object] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            if any(part in key.casefold() for part in _SENSITIVE_PARTS):
                continue
            result[key] = _safe_payload(raw_value)
        return result
    if isinstance(value, list):
        return [_safe_payload(item) for item in value]
    return value


def _belongs_to_channel(event: DomainEvent, channel_profile_id: uuid.UUID) -> bool:
    wanted = str(channel_profile_id)
    if event.aggregate_type == "channel_profile" and event.aggregate_id == wanted:
        return True
    payload = dict(event.payload or {})
    return str(payload.get("channel_profile_id") or "") == wanted


def _scope_value(channel_profile_id: uuid.UUID | None) -> str | None:
    return str(channel_profile_id) if channel_profile_id is not None else None


def _cursor_scope(cursor: EventConsumerCursor) -> str | None:
    metadata = dict(cursor.cursor_metadata or {})
    value = metadata.get(_SCOPE_KEY)
    return str(value) if value is not None else None


def _require_cursor_scope(
    cursor: EventConsumerCursor | None,
    channel_profile_id: uuid.UUID | None,
) -> None:
    if cursor is None:
        return
    expected = _scope_value(channel_profile_id)
    actual = _cursor_scope(cursor)
    if actual != expected:
        raise ValueError(
            "consumer cursor is bound to a different channel scope; use a distinct "
            "consumer_key for each channel or for the unscoped stream"
        )


def _after_cursor_query(
    last_created_at: datetime | None,
    last_event_id: uuid.UUID | None,
):
    stmt = select(DomainEvent)
    if last_created_at is None:
        return stmt
    if last_event_id is None:
        return stmt.where(DomainEvent.created_at > last_created_at)
    return stmt.where(
        or_(
            DomainEvent.created_at > last_created_at,
            and_(
                DomainEvent.created_at == last_created_at,
                DomainEvent.id > last_event_id,
            ),
        )
    )


def list_consumer_events(
    consumer_key: str,
    *,
    channel_profile_id: uuid.UUID | None = None,
    limit: int = 100,
) -> list[dict[str, object]]:
    key = consumer_key.strip()
    if not key or len(key) > 128:
        raise ValueError("consumer_key must contain 1-128 characters")
    if limit < 1 or limit > 500:
        raise ValueError("event limit must be between 1 and 500")

    with session_scope() as session:
        cursor = session.get(EventConsumerCursor, key)
        if cursor is None:
            cursor = EventConsumerCursor(
                consumer_key=key,
                last_event_id=None,
                last_event_created_at=None,
                cursor_metadata={_SCOPE_KEY: _scope_value(channel_profile_id)},
            )
            session.add(cursor)
            session.flush()
        _require_cursor_scope(cursor, channel_profile_id)
        scan_created = cursor.last_event_created_at
        scan_id = cursor.last_event_id
        selected: list[DomainEvent] = []

        for _ in range(_MAX_SCAN_BATCHES):
            rows = list(
                session.scalars(
                    _after_cursor_query(scan_created, scan_id)
                    .order_by(DomainEvent.created_at, DomainEvent.id)
                    .limit(_SCAN_BATCH)
                )
            )
            if not rows:
                break
            if channel_profile_id is None:
                selected.extend(rows[: limit - len(selected)])
            else:
                for row in rows:
                    if _belongs_to_channel(row, channel_profile_id):
                        selected.append(row)
                        if len(selected) >= limit:
                            break
            if len(selected) >= limit:
                break
            last = rows[-1]
            scan_created = last.created_at
            scan_id = last.id
            if len(rows) < _SCAN_BATCH:
                break

        return [
            {
                "id": row.id,
                "event_type": row.event_type,
                "aggregate_type": row.aggregate_type,
                "aggregate_id": row.aggregate_id,
                "payload": _safe_payload(dict(row.payload or {})),
                "created_at": row.created_at,
            }
            for row in selected[:limit]
        ]


def acknowledge_consumer_event(
    consumer_key: str,
    event_id: uuid.UUID,
    *,
    channel_profile_id: uuid.UUID | None = None,
    metadata: dict[str, object] | None = None,
) -> EventConsumerCursor:
    key = consumer_key.strip()
    if not key or len(key) > 128:
        raise ValueError("consumer_key must contain 1-128 characters")

    with session_scope() as session:
        event = session.get(DomainEvent, event_id)
        if event is None:
            raise ValueError(f"domain event not found: {event_id}")
        cursor = session.get(EventConsumerCursor, key)
        if cursor is not None and channel_profile_id is None:
            stored_scope = _cursor_scope(cursor)
            if stored_scope is not None:
                channel_profile_id = uuid.UUID(stored_scope)
        _require_cursor_scope(cursor, channel_profile_id)
        if channel_profile_id is not None and not _belongs_to_channel(
            event,
            channel_profile_id,
        ):
            raise ValueError("domain event does not belong to the consumer channel scope")

        scope = _scope_value(channel_profile_id)
        safe_metadata = dict(metadata or {})
        safe_metadata.pop(_SCOPE_KEY, None)
        safe_metadata[_SCOPE_KEY] = scope

        if cursor is None:
            cursor = EventConsumerCursor(
                consumer_key=key,
                last_event_id=event.id,
                last_event_created_at=event.created_at,
                cursor_metadata=safe_metadata,
            )
            session.add(cursor)
        else:
            current = (
                cursor.last_event_created_at,
                str(cursor.last_event_id or ""),
            )
            candidate = (event.created_at, str(event.id))
            if cursor.last_event_created_at is None or candidate > current:
                cursor.last_event_id = event.id
                cursor.last_event_created_at = event.created_at
            if metadata is not None:
                cursor.cursor_metadata = safe_metadata
        session.flush()
        session.refresh(cursor)
        session.expunge(cursor)
        return cursor
