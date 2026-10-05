"""Authenticated control-plane principals, scopes, and channel boundaries."""

from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from katcha.config import (
    ControlCredentialSettings,
    ControlPrincipalSettings,
    Settings,
    get_settings,
)

_bearer = HTTPBearer(auto_error=False)
_PUBLIC_PATHS = {
    "/v1/health/live",
    "/v1/health/ready",
    "/v1/integrations/youtube/oauth/callback",
    "/auth/callback",
}


def _token_actor(token: str) -> str:
    digest = hashlib.sha256(token.encode()).hexdigest()[:12]
    return f"control-token:{digest}"


def _principal_actor(name: str) -> str:
    return f"control-principal:{name}"


def _credential_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:12]


@dataclass(frozen=True, slots=True)
class _PrincipalCredentialMatch:
    principal: ControlPrincipalSettings
    credential: ControlCredentialSettings


def _credential_is_active(
    credential: ControlCredentialSettings,
    *,
    now: datetime,
) -> bool:
    if credential.disabled:
        return False
    if credential.not_before is not None and now < credential.not_before:
        return False
    return credential.expires_at is None or now < credential.expires_at


def _set_identity(
    request: Request,
    *,
    actor: str,
    scopes: set[str],
    channel_profile_ids: set[str],
    principal_name: str | None,
    credential_id: str | None = None,
    credential_fingerprint: str | None = None,
    credential_not_before: datetime | None = None,
    credential_expires_at: datetime | None = None,
) -> None:
    request.state.control_actor = actor
    request.state.control_scopes = set(scopes)
    request.state.control_channel_profile_ids = set(channel_profile_ids)
    request.state.control_principal_name = principal_name
    request.state.control_credential_id = credential_id
    request.state.control_credential_fingerprint = credential_fingerprint
    request.state.control_credential_not_before = credential_not_before
    request.state.control_credential_expires_at = credential_expires_at


def _match_principal_credential(
    settings: Settings,
    presented_token: str,
    *,
    now: datetime | None = None,
) -> _PrincipalCredentialMatch | None:
    matched: _PrincipalCredentialMatch | None = None
    current = now or datetime.now(UTC)
    for principal in settings.control_principals:
        for credential in principal.resolved_credentials():
            token = credential.token.get_secret_value()
            active = _credential_is_active(credential, now=current)
            same = secrets.compare_digest(
                presented_token.encode(),
                token.encode(),
            )
            if same and active:
                matched = _PrincipalCredentialMatch(
                    principal=principal,
                    credential=credential,
                )
    return matched


def _authenticate(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None,
    settings: Settings,
) -> None:
    if settings.control_principals:
        if credentials is None:
            raise HTTPException(
                status_code=401,
                detail="control-plane authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        match = _match_principal_credential(
            settings,
            credentials.credentials,
        )
        if match is None:
            raise HTTPException(
                status_code=401,
                detail="control-plane authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        _set_identity(
            request,
            actor=_principal_actor(match.principal.name),
            scopes=set(match.principal.scopes),
            channel_profile_ids=set(match.principal.channel_profile_ids),
            principal_name=match.principal.name,
            credential_id=match.credential.id,
            credential_fingerprint=_credential_fingerprint(credentials.credentials),
            credential_not_before=match.credential.not_before,
            credential_expires_at=match.credential.expires_at,
        )
        return

    expected = settings.control_api_token
    if expected is None:
        if settings.env == "production":
            raise HTTPException(
                status_code=503,
                detail="control-plane authentication not configured",
            )
        _set_identity(
            request,
            actor="local-development",
            scopes={"*"},
            channel_profile_ids={"*"},
            principal_name=None,
            credential_id=None,
            credential_fingerprint=None,
        )
        return

    if credentials is None or not secrets.compare_digest(
        credentials.credentials.encode(),
        expected.get_secret_value().encode(),
    ):
        raise HTTPException(
            status_code=401,
            detail="control-plane authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    _set_identity(
        request,
        actor=_token_actor(credentials.credentials),
        scopes=settings.resolved_control_scopes(),
        channel_profile_ids={"*"},
        principal_name=None,
        credential_id="legacy-control-api-token",
        credential_fingerprint=_credential_fingerprint(credentials.credentials),
    )


def _require_named_principal_route_access(request: Request) -> None:
    if control_principal_name(request) is None:
        return

    scopes = set(getattr(request.state, "control_scopes", set()))
    if "*" in scopes:
        return

    path = request.url.path
    method = request.method.upper()

    if path.startswith("/v1/ai/actions/") and path.endswith("/execute"):
        # The action endpoint resolves the proposal first and enforces the
        # action-specific scope before claiming or executing it.
        return

    if path.startswith("/v1/discovery/sources/") and path.endswith("/polling"):
        require_control_scope(request, "discovery:write")
        return
    if path in {"/v1/publications", "/v1/productions"} and method == "GET":
        require_control_scope(request, "channels:read")
        channel = request.query_params.get("channel_profile_id")
        if path == "/v1/publications":
            from sqlalchemy import select

            from katcha.db import session_scope
            from katcha.intelligence_models import ChannelProfile

            try:
                connection_id = uuid.UUID(request.query_params.get("youtube_connection_id", ""))
            except ValueError as exc:
                raise HTTPException(
                    403, "Choose a channel connection for publication retrieval"
                ) from exc
            with session_scope() as session:
                channel = session.scalar(
                    select(ChannelProfile.id).where(
                        ChannelProfile.youtube_connection_id == connection_id
                    )
                )
        if not channel:
            raise HTTPException(403, "Choose a channel for resource retrieval")
        require_control_channel(request, channel)
        return
    if path == "/v1/control/session":
        return

    if path == "/v1/operations/overview":
        require_control_scope(request, "channels:read")
        return

    if path.startswith("/v1/publications/") and path.endswith(
        ("/draft", "/release-plan", "/thumbnail", "/reconcile-release", "/start")
    ):
        require_control_scope(request, "production:create")
        return  # The publication router verifies the exact resource channel before mutation.

    if path == "/v1/ai/goals" and method == "POST":
        require_control_scope(request, "ai:command")
        return
    if path.startswith("/v1/ai/goals/") and path.endswith("/cancel"):
        require_control_scope(request, "ai:write")
        return
    if path == "/v1/ai/command":
        require_control_scope(request, "ai:command")
        return
    if path.startswith("/v1/ai/threads/") and path.endswith("/archive"):
        require_control_scope(request, "ai:write")
        return
    if path.startswith("/v1/ai/"):
        require_control_scope(request, "ai:read")
        return

    if path == "/v1/control/events":
        require_control_scope(request, "events:read")
        return
    if path.startswith("/v1/control/events/") and path.endswith("/ack"):
        require_control_scope(request, "events:ack")
        return

    if path == "/v1/channels":
        require_control_scope(
            request,
            "channels:read" if method in {"GET", "HEAD"} else "channels:write",
        )
        return

    if re.fullmatch(
        r"/v1/channels/[^/]+/editorial-projects/[^/]+/(?:narration|images)(?:/[^/]+/revoke)?", path
    ):
        require_control_scope(request, "ai:read" if method == "GET" else "production:create")
        return

    if method == "GET" and re.fullmatch(
        r"/v1/channels/[^/]+/editorial-projects/[^/]+/runs/[^/]+/preview", path
    ):
        # Binary playback shares editorial read scope; the router checks channel,
        # project, frozen revision and current clearance before streaming.
        require_control_scope(request, "ai:read")
        return

    from katcha.services.goal_tools import native_route_tool, require_native_resource_channels

    tool = native_route_tool(path, method)
    if tool is not None:
        require_control_scope(request, tool.scope)
        try:
            require_native_resource_channels(request, tool)
        except ValueError as exc:
            raise HTTPException(403, str(exc)) from exc
        return

    if path.startswith("/v1/channels/"):
        if "/content" in path:
            # Resource/channel and mutation-specific scopes are checked by the router.
            require_control_scope(
                request,
                "channels:read"
                if method in {"GET", "HEAD"}
                else (
                    "production:create"
                    if path.endswith(("/prepare", "/publication"))
                    else "channels:write"
                ),
            )
            return
        if "/trends/" in path or path.endswith("/trends"):
            require_control_scope(
                request,
                "trends:read" if method in {"GET", "HEAD"} else "trends:write",
            )
            return
        if path.endswith("/intelligence/refresh") or path.endswith(
            ("/strategy", "/growth-goals", "/automation/promote")
        ):
            require_control_scope(request, "intelligence:write")
            return
        if "/clips/" in path and path.endswith("/productions"):
            require_control_scope(request, "production:create")
            return
        if path.endswith("/compilations"):
            require_control_scope(request, "production:create")
            return
        require_control_scope(
            request,
            "channels:read" if method in {"GET", "HEAD"} else "channels:write",
        )
        return

    raise HTTPException(
        status_code=403,
        detail=(
            "restricted control principals cannot access this API route; "
            "use a wildcard operator principal or add an explicit scoped "
            "control-plane contract"
        ),
    )


def require_control_token(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> None:
    if not request.url.path.startswith("/v1/") or request.url.path in _PUBLIC_PATHS:
        return

    _authenticate(request, credentials, get_settings())

    path_channel = request.path_params.get("channel_profile_id")
    if path_channel is not None:
        require_control_channel(request, path_channel)

    _require_named_principal_route_access(request)


def control_actor(request: Request) -> str:
    actor = getattr(request.state, "control_actor", None)
    if not actor:
        raise HTTPException(
            status_code=401,
            detail="authenticated control actor required",
        )
    return str(actor)


def control_principal_name(request: Request) -> str | None:
    value = getattr(request.state, "control_principal_name", None)
    return str(value) if value else None


def control_credential_id(request: Request) -> str | None:
    value = getattr(request.state, "control_credential_id", None)
    return str(value) if value else None


def control_credential_fingerprint(request: Request) -> str | None:
    value = getattr(request.state, "control_credential_fingerprint", None)
    return str(value) if value else None


def control_credential_not_before(request: Request) -> datetime | None:
    value = getattr(request.state, "control_credential_not_before", None)
    return value if isinstance(value, datetime) else None


def control_credential_expires_at(request: Request) -> datetime | None:
    value = getattr(request.state, "control_credential_expires_at", None)
    return value if isinstance(value, datetime) else None


def control_scopes(request: Request) -> set[str]:
    scopes = set(getattr(request.state, "control_scopes", set()))
    if not scopes:
        raise HTTPException(
            status_code=401,
            detail="authenticated control scopes required",
        )
    return scopes


def require_control_scope(request: Request, scope: str) -> None:
    scopes = control_scopes(request)
    if "*" in scopes or scope in scopes:
        return
    raise HTTPException(
        status_code=403,
        detail=f"control-plane scope required: {scope}",
    )


def require_control_scope_any(request: Request, *scopes: str) -> None:
    granted = control_scopes(request)
    if "*" in granted or any(scope in granted for scope in scopes):
        return
    wanted = ", ".join(scopes)
    raise HTTPException(
        status_code=403,
        detail=f"one control-plane scope is required: {wanted}",
    )


def control_allowed_channel_ids(
    request: Request,
) -> set[uuid.UUID] | None:
    raw = set(
        getattr(
            request.state,
            "control_channel_profile_ids",
            set(),
        )
    )
    if "*" in raw:
        return None
    if not raw:
        raise HTTPException(
            status_code=403,
            detail="control principal has no channel access",
        )
    return {uuid.UUID(str(value)) for value in raw}


def require_control_channel(
    request: Request,
    channel_profile_id: uuid.UUID | str,
) -> None:
    allowed = control_allowed_channel_ids(request)
    if allowed is None:
        return
    try:
        channel_id = (
            channel_profile_id
            if isinstance(channel_profile_id, uuid.UUID)
            else uuid.UUID(str(channel_profile_id))
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=403,
            detail="control principal is not authorized for this channel",
        ) from exc
    if channel_id in allowed:
        return
    raise HTTPException(
        status_code=403,
        detail="control principal is not authorized for this channel",
    )


async def require_native_channel_body(request: Request) -> None:
    """Scoped native tools enforce channel ownership for path, query and body."""
    if control_principal_name(request) is None or "*" in control_scopes(request):
        return
    from katcha.services.goal_tools import native_route_tool

    tool = native_route_tool(request.url.path, request.method.upper())
    if tool is None:
        return
    if tool.name in {"list_sources", "source_library", "clip_library"}:
        channel = request.query_params.get("channel_profile_id")
        if not channel:
            raise HTTPException(403, "A channel is required for scoped resource retrieval")
        require_control_channel(request, channel)
    if tool.name == "save_source":
        body = await request.json()
        channel = body.get("channel_profile_id")
        if not channel:
            raise HTTPException(403, "Shared resource changes require a wildcard principal")
        require_control_channel(request, channel)
        if tool.name == "save_source":
            from sqlalchemy import select

            from katcha.acquisition_models import IngestionSource
            from katcha.db import session_scope

            with session_scope() as session:
                source = session.scalar(
                    select(IngestionSource).where(
                        IngestionSource.source_key == body.get("source_key")
                    )
                )
                if source and source.channel_profile_id != uuid.UUID(channel):
                    raise HTTPException(403, "Cannot replace a shared or other channel source")
