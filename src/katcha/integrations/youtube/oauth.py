from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx
from sqlalchemy import delete, select

from katcha.config import Settings, get_settings
from katcha.db import session_scope
from katcha.domain import YouTubeConnectionStatus
from katcha.models import DomainEvent
from katcha.publishing_models import OAuthState, YouTubeConnection
from katcha.security.secrets import decrypt_secret, encrypt_secret

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
YOUTUBE_CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"

BASE_YOUTUBE_SCOPES = (
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
)
MONETARY_SCOPE = "https://www.googleapis.com/auth/yt-analytics-monetary.readonly"


class YouTubeOAuthError(RuntimeError):
    pass


def requested_scopes(settings: Settings | None = None) -> tuple[str, ...]:
    settings = settings or get_settings()
    scopes = list(BASE_YOUTUBE_SCOPES)
    if settings.youtube_include_monetary_scope:
        scopes.append(MONETARY_SCOPE)
    return tuple(scopes)


def _require_oauth_settings(settings: Settings) -> None:
    if not settings.youtube_client_id or not settings.youtube_client_secret:
        raise YouTubeOAuthError(
            "KATCHA_YOUTUBE_CLIENT_ID and KATCHA_YOUTUBE_CLIENT_SECRET are required"
        )
    if not settings.credential_encryption_key:
        raise YouTubeOAuthError("KATCHA_CREDENTIAL_ENCRYPTION_KEY is required")


def _state_hash(state: str) -> str:
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def build_authorization_url(
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
    scopes: tuple[str, ...],
) -> str:
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(scopes),
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{GOOGLE_AUTH_URL}?{query}"


def begin_youtube_oauth(settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    _require_oauth_settings(settings)
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    now = datetime.now(UTC)
    expires_at = now + timedelta(minutes=10)

    with session_scope() as session:
        session.execute(
            delete(OAuthState).where(
                OAuthState.expires_at < now - timedelta(hours=1),
                OAuthState.consumed_at.is_not(None),
            )
        )
        session.add(
            OAuthState(
                state_hash=_state_hash(state),
                encrypted_code_verifier=encrypt_secret(verifier, settings),
                redirect_uri=settings.youtube_redirect_uri,
                expires_at=expires_at,
            )
        )

    return build_authorization_url(
        client_id=settings.youtube_client_id or "",
        redirect_uri=settings.youtube_redirect_uri,
        state=state,
        code_challenge=_pkce_challenge(verifier),
        scopes=requested_scopes(settings),
    )


def _exchange_code(
    code: str,
    verifier: str,
    redirect_uri: str,
    settings: Settings,
) -> dict[str, object]:
    response = httpx.post(
        GOOGLE_TOKEN_URL,
        data={
            "client_id": settings.youtube_client_id,
            "client_secret": settings.youtube_client_secret,
            "code": code,
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
        timeout=30,
    )
    if response.is_error:
        raise YouTubeOAuthError(f"Google token exchange failed: {response.text[:1000]}")
    payload = response.json()
    if not payload.get("access_token"):
        raise YouTubeOAuthError("Google token response did not include an access token")
    return payload


def _channel_identity(access_token: str) -> tuple[str, str, dict[str, object]]:
    response = httpx.get(
        YOUTUBE_CHANNELS_URL,
        params={"part": "snippet", "mine": "true"},
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    if response.is_error:
        raise YouTubeOAuthError(f"YouTube channel lookup failed: {response.text[:1000]}")
    payload = response.json()
    items = payload.get("items") or []
    if not items:
        raise YouTubeOAuthError("authorized Google account has no accessible YouTube channel")
    channel = items[0]
    channel_id = str(channel.get("id") or "")
    title = str((channel.get("snippet") or {}).get("title") or channel_id)
    if not channel_id:
        raise YouTubeOAuthError("YouTube channel response did not include a channel ID")
    return channel_id, title, payload


def complete_youtube_oauth(
    *,
    state: str,
    code: str,
    settings: Settings | None = None,
) -> YouTubeConnection:
    settings = settings or get_settings()
    _require_oauth_settings(settings)
    now = datetime.now(UTC)
    state_digest = _state_hash(state)

    with session_scope() as session:
        oauth_state = session.scalar(
            select(OAuthState).where(OAuthState.state_hash == state_digest)
        )
        if oauth_state is None:
            raise YouTubeOAuthError("OAuth state is unknown")
        if oauth_state.consumed_at is not None:
            raise YouTubeOAuthError("OAuth state has already been consumed")
        if oauth_state.expires_at <= now:
            raise YouTubeOAuthError("OAuth state has expired")
        verifier = decrypt_secret(oauth_state.encrypted_code_verifier, settings)
        redirect_uri = oauth_state.redirect_uri

    token = _exchange_code(code, verifier, redirect_uri, settings)
    access_token = str(token["access_token"])
    channel_id, channel_title, channel_payload = _channel_identity(access_token)
    expires_in = int(token.get("expires_in") or 3600)
    token_expires_at = now + timedelta(seconds=expires_in)
    raw_scopes = str(token.get("scope") or " ".join(requested_scopes(settings)))
    scopes = sorted(set(raw_scopes.split()))
    refresh_token = token.get("refresh_token")

    with session_scope() as session:
        oauth_state = session.scalar(
            select(OAuthState).where(OAuthState.state_hash == state_digest).with_for_update()
        )
        if oauth_state is None or oauth_state.consumed_at is not None:
            raise YouTubeOAuthError("OAuth state was consumed concurrently")

        connection = session.scalar(
            select(YouTubeConnection)
            .where(YouTubeConnection.channel_id == channel_id)
            .with_for_update()
        )
        if connection is None:
            if not refresh_token:
                raise YouTubeOAuthError(
                    "Google did not issue a refresh token; reconnect with consent enabled"
                )
            connection = YouTubeConnection(
                channel_id=channel_id,
                channel_title=channel_title,
                status=YouTubeConnectionStatus.ACTIVE.value,
                scopes=scopes,
                encrypted_access_token=encrypt_secret(access_token, settings),
                encrypted_refresh_token=encrypt_secret(str(refresh_token), settings),
                token_expires_at=token_expires_at,
                connection_metadata={"channel_response": channel_payload},
            )
            session.add(connection)
        else:
            connection.channel_title = channel_title
            connection.status = YouTubeConnectionStatus.ACTIVE.value
            connection.scopes = scopes
            connection.encrypted_access_token = encrypt_secret(access_token, settings)
            if refresh_token:
                connection.encrypted_refresh_token = encrypt_secret(str(refresh_token), settings)
            connection.token_expires_at = token_expires_at
            connection.connection_metadata = {"channel_response": channel_payload}

        oauth_state.consumed_at = now
        session.flush()
        session.add(
            DomainEvent(
                aggregate_type="youtube_connection",
                aggregate_id=str(connection.id),
                event_type="youtube.connected",
                payload={
                    "connection_id": str(connection.id),
                    "channel_id": channel_id,
                    "channel_title": channel_title,
                    "scopes": scopes,
                },
            )
        )
        session.refresh(connection)
        session.expunge(connection)
        return connection
