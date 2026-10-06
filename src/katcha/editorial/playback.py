"""Short-lived same-origin playback grants for acquired Editorial footage."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete

from katcha.db import session_scope
from katcha.editorial.source_monitor import source_monitor
from katcha.editorial_models import EditorialPlaybackTicket
from katcha.services.editorial_projects import EditorialConflict

PLAYBACK_TTL_SECONDS = 600


@dataclass(frozen=True, slots=True)
class PlaybackGrant:
    id: uuid.UUID
    token: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class PlaybackSource:
    storage_key: str
    filename: str
    clip_id: uuid.UUID


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def playback_cookie_name(ticket_id: uuid.UUID) -> str:
    return f"katcha_playback_{ticket_id.hex}"


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue_playback_grant(
    channel_id: uuid.UUID,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    candidate_id: str,
    *,
    actor: str,
    now: datetime | None = None,
) -> PlaybackGrant:
    evidence = source_monitor(channel_id, project_id, run_id, candidate_id)
    current = _utc(now or datetime.now(UTC))
    expires_at = current + timedelta(seconds=PLAYBACK_TTL_SECONDS)
    ticket_id = uuid.uuid4()
    token = secrets.token_urlsafe(32)
    with session_scope() as session:
        session.execute(
            delete(EditorialPlaybackTicket).where(
                EditorialPlaybackTicket.expires_at <= current
            )
        )
        session.add(
            EditorialPlaybackTicket(
                id=ticket_id,
                channel_profile_id=channel_id,
                project_id=project_id,
                run_id=run_id,
                clip_id=uuid.UUID(str(evidence["clip_id"])),
                candidate_id=candidate_id,
                token_digest=_token_digest(token),
                actor=actor,
                expires_at=expires_at,
            )
        )
    return PlaybackGrant(id=ticket_id, token=token, expires_at=expires_at)


def resolve_playback_grant(
    ticket_id: uuid.UUID,
    token: str | None,
    *,
    now: datetime | None = None,
) -> PlaybackSource:
    current = now or datetime.now(UTC)
    with session_scope() as session:
        ticket = session.get(EditorialPlaybackTicket, ticket_id)
        if ticket is None:
            raise EditorialConflict("Playback ticket is not available")
        if _utc(ticket.expires_at) <= current:
            session.delete(ticket)
            raise EditorialConflict("Playback ticket expired")
        if not token or not secrets.compare_digest(
            ticket.token_digest,
            _token_digest(token),
        ):
            raise EditorialConflict("Playback ticket is invalid")
        channel_id = ticket.channel_profile_id
        project_id = ticket.project_id
        run_id = ticket.run_id
        candidate_id = ticket.candidate_id
        clip_id = ticket.clip_id

    evidence = source_monitor(channel_id, project_id, run_id, candidate_id)
    if str(clip_id) != str(evidence["clip_id"]):
        raise EditorialConflict("Playback source changed; request a new ticket")
    return PlaybackSource(
        storage_key=str(evidence["media_storage_key"]),
        filename=f"editorial-source-{candidate_id}.{evidence['extension']}",
        clip_id=clip_id,
    )
