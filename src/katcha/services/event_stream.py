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
        last_created = cursor.last_event_created_at if cursor else None
        last_id = cursor.last_event_id if cursor else None
        fetch_limit = min(5000, max(limit * 10, 250))
        rows = list(
            session.scalars(
                _after_cursor_query(last_created, last_id)
                .order_by(DomainEvent.created_at, DomainEvent.id)
                .limit(fetch_limit)
            )
        )
        if channel_profile_id is not None:
            rows = [
                row
                for row in rows
                if _belongs_to_channel(row, channel_profile_id)
            ]
        return [
            {
                "id": row.id,
                "event_type": row.event_type,
                "aggregate_type": row.aggregate_type,
                "aggregate_id": row.aggregate_id,
                "payload": _safe_payload(dict(row.payload or {})),
                "created_at": row.created_at,
            }
            for row in rows[:limit]
        ]


def acknowledge_consumer_event(
    consumer_key: str,
    event_id: uuid.UUID,
    *,
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
        if cursor is None:
            cursor = EventConsumerCursor(
                consumer_key=key,
                last_event_id=event.id,
                last_event_created_at=event.created_at,
                cursor_metadata=dict(metadata or {}),
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
                cursor.cursor_metadata = dict(metadata)
        session.flush()
        session.refresh(cursor)
        session.expunge(cursor)
        return cursor
