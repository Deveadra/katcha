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


def require_control_scope(request: Request, scope: str) -> None:
    scopes = set(getattr(request.state, "control_scopes", set()))
    if "*" in scopes or scope in scopes:
        return
    raise HTTPException(
        status_code=403,
        detail=f"control-plane scope required: {scope}",
    )


def require_control_scope_any(request: Request, *scopes: str) -> None:
    granted = set(getattr(request.state, "control_scopes", set()))
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
