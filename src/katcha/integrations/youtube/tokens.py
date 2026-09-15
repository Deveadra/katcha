from __future__ import annotations

import uuid
from contextlib import suppress
from datetime import UTC, datetime, timedelta

import httpx

from katcha.config import Settings, get_settings
from katcha.db import session_scope
from katcha.domain import YouTubeConnectionStatus
from katcha.models import DomainEvent
from katcha.publishing_models import YouTubeConnection
from katcha.security.secrets import decrypt_secret, encrypt_secret

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"


class YouTubeCredentialError(RuntimeError):
    pass


def get_valid_access_token(
    connection_id: uuid.UUID,
    *,
    settings: Settings | None = None,
) -> str:
    settings = settings or get_settings()
    now = datetime.now(UTC)

    with session_scope() as session:
        connection = session.get(YouTubeConnection, connection_id)
        if connection is None:
            raise YouTubeCredentialError(f"YouTube connection not found: {connection_id}")
        if connection.status != YouTubeConnectionStatus.ACTIVE.value:
            raise YouTubeCredentialError("YouTube connection is not active")
        if connection.token_expires_at > now + timedelta(minutes=5):
            return decrypt_secret(connection.encrypted_access_token, settings)
        refresh_token = decrypt_secret(connection.encrypted_refresh_token, settings)

    if not settings.youtube_client_id or not settings.youtube_client_secret:
        raise YouTubeCredentialError("YouTube OAuth client is not configured")

    response = httpx.post(
        GOOGLE_TOKEN_URL,
        data={
            "client_id": settings.youtube_client_id,
            "client_secret": settings.youtube_client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    if response.is_error:
        error_code = ""
        with suppress(ValueError):
            error_code = str(response.json().get("error") or "")
        if response.status_code == 401 or error_code == "invalid_grant":
            with session_scope() as session:
                connection = session.get(YouTubeConnection, connection_id)
                if connection is not None:
                    connection.status = YouTubeConnectionStatus.REVOKED.value
                    session.add(
                        DomainEvent(
                            aggregate_type="youtube_connection",
                            aggregate_id=str(connection_id),
                            event_type="youtube.credentials_revoked",
                            payload={"connection_id": str(connection_id)},
                        )
                    )
        raise YouTubeCredentialError(f"YouTube token refresh failed: {response.text[:1000]}")

    payload = response.json()
    access_token = str(payload.get("access_token") or "")
    if not access_token:
        raise YouTubeCredentialError("Google refresh response did not include an access token")
    expires_in = int(payload.get("expires_in") or 3600)

    with session_scope() as session:
        connection = session.get(YouTubeConnection, connection_id)
        if connection is None:
            raise YouTubeCredentialError("YouTube connection disappeared during refresh")
        connection.encrypted_access_token = encrypt_secret(access_token, settings)
        connection.token_expires_at = now + timedelta(seconds=expires_in)
        connection.last_refreshed_at = now
        if payload.get("scope"):
            connection.scopes = sorted(set(str(payload["scope"]).split()))
        connection.status = YouTubeConnectionStatus.ACTIVE.value
        session.add(
            DomainEvent(
                aggregate_type="youtube_connection",
                aggregate_id=str(connection_id),
                event_type="youtube.token_refreshed",
                payload={"connection_id": str(connection_id)},
            )
        )
    return access_token
