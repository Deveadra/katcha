"""Single-operator control-plane credential shared by GUI and machine clients."""

from __future__ import annotations

import hashlib
import secrets
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from katcha.config import get_settings

_bearer = HTTPBearer(auto_error=False)


def _token_actor(token: str) -> str:
    digest = hashlib.sha256(token.encode()).hexdigest()[:12]
    return f"control-token:{digest}"


def require_control_token(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> None:
    if not request.url.path.startswith("/v1/") or request.url.path in {
        "/v1/health/live",
        "/v1/health/workspace",
        "/v1/health/ready",
        "/v1/integrations/youtube/oauth/callback",
    }:
        return
    settings = get_settings()
    expected = settings.control_api_token
    if expected is None:
        if settings.env == "production":
            raise HTTPException(
                status_code=503, detail="control-plane authentication not configured"
            )
        request.state.control_actor = "local-development"
        request.state.control_scopes = {"*"}
        return
    if credentials is None or not secrets.compare_digest(
        credentials.credentials.encode(), expected.get_secret_value().encode()
    ):
        raise HTTPException(
            status_code=401,
            detail="control-plane authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    request.state.control_actor = _token_actor(credentials.credentials)
    request.state.control_scopes = settings.resolved_control_scopes()


def control_actor(request: Request) -> str:
    actor = getattr(request.state, "control_actor", None)
    if not actor:
        raise HTTPException(status_code=401, detail="authenticated control actor required")
    return str(actor)


def require_control_scope(request: Request, scope: str) -> None:
    scopes = set(getattr(request.state, "control_scopes", set()))
    if "*" in scopes or scope in scopes:
        return
    raise HTTPException(
        status_code=403,
        detail=f"control-plane scope required: {scope}",
    )
