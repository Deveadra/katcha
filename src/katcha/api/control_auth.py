"""Single-operator control-plane credential shared by GUI and machine clients."""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from katcha.config import get_settings

_bearer = HTTPBearer(auto_error=False)


def require_control_token(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> None:
    if not request.url.path.startswith("/v1/") or request.url.path in {
        "/v1/health/live",
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
        return  # Local development only; bind to loopback or a private network.
    if credentials is None or not secrets.compare_digest(
        credentials.credentials.encode(), expected.get_secret_value().encode()
    ):
        raise HTTPException(
            status_code=401,
            detail="control-plane authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
