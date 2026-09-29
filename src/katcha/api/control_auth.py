"""Authenticated control-plane principals, scopes, and channel boundaries."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from katcha.config import ControlPrincipalSettings, Settings, get_settings

_bearer = HTTPBearer(auto_error=False)
_PUBLIC_PATHS = {
    "/v1/health/live",
    "/v1/health/workspace",
    "/v1/health/ready",
    "/v1/integrations/youtube/oauth/callback",
}


def _token_actor(token: str) -> str:
    digest = hashlib.sha256(token.encode()).hexdigest()[:12]
    return f"control-token:{digest}"


def _principal_actor(name: str) -> str:
    return f"control-principal:{name}"


def _set_identity(
    request: Request,
    *,
    actor: str,
    scopes: set[str],
    channel_profile_ids: set[str],
    principal_name: str | None,
) -> None:
    request.state.control_actor = actor
    request.state.control_scopes = set(scopes)
    request.state.control_channel_profile_ids = set(channel_profile_ids)
    request.state.control_principal_name = principal_name


def _match_principal(
    settings: Settings,
    presented_token: str,
) -> ControlPrincipalSettings | None:
    matched: ControlPrincipalSettings | None = None
    for principal in settings.control_principals:
        same = secrets.compare_digest(
            presented_token.encode(),
            principal.token.get_secret_value().encode(),
        )
        if same:
            matched = principal
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
        principal = _match_principal(settings, credentials.credentials)
        if principal is None:
            raise HTTPException(
                status_code=401,
                detail="control-plane authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        _set_identity(
            request,
            actor=_principal_actor(principal.name),
            scopes=set(principal.scopes),
            channel_profile_ids=set(principal.channel_profile_ids),
            principal_name=principal.name,
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

    if path == "/v1/control/session":
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

    if path.startswith("/v1/channels/"):
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
