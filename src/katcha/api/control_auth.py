"""Authenticated control-plane principals, scopes, and channel boundaries."""

from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
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
    "/v1/health/release",
    "/v1/control/access-login",
    "/v1/integrations/youtube/oauth/callback",
    "/auth/callback",
}
_EDITORIAL_SOURCE_MEDIA_PATH = re.compile(
    r"/v1/channels/[^/]+/editorial-projects/[^/]+/runs/[^/]+/assets/[^/]+/source-media$"
)


_REVIEW_MEDIA_PATH = re.compile(
    r"/v1/(?:productions/[^/]+|clips/[^/]+)/media$"
)
_EDITORIAL_REVIEW_MEDIA_PATH = re.compile(
    r"/v1/channels/[^/]+/editorial-projects/[^/]+/runs/[^/]+/preview$"
)


def _uses_review_media_ticket(request: Request) -> bool:
    from katcha.security.media_tickets import REVIEW_MEDIA_COOKIE

    return (
        request.method.upper() == "GET"
        and bool(
            _REVIEW_MEDIA_PATH.fullmatch(request.url.path)
            or _EDITORIAL_REVIEW_MEDIA_PATH.fullmatch(request.url.path)
        )
        and bool(request.cookies.get(REVIEW_MEDIA_COOKIE))
    )


def _uses_editorial_media_ticket(request: Request) -> bool:
    return request.method.upper() == "GET" and bool(
        _EDITORIAL_SOURCE_MEDIA_PATH.fullmatch(request.url.path)
    )


def _token_actor(token: str) -> str:
    digest = hashlib.sha256(token.encode()).hexdigest()[:12]
    return f"control-token:{digest}"


def _principal_actor(name: str) -> str:
    return f"control-principal:{name}"


def _credential_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:12]


@lru_cache(maxsize=8)
def _cloudflare_access_jwks_client(team_domain: str):
    import jwt

    return jwt.PyJWKClient(f"{team_domain}/cdn-cgi/access/certs")


def _authenticate_cloudflare_access(request: Request, settings: Settings) -> None:
    team_domain = settings.cloudflare_access_team_domain
    audience = settings.cloudflare_access_audience
    if not team_domain or not audience:
        raise HTTPException(
            status_code=503,
            detail="Cloudflare Access authentication is not configured",
        )

    assertion = request.headers.get("Cf-Access-Jwt-Assertion")
    if not assertion:
        raise HTTPException(
            status_code=401,
            detail="control-plane authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        import jwt

        signing_key = _cloudflare_access_jwks_client(
            team_domain
        ).get_signing_key_from_jwt(assertion)
    except ImportError as exc:
        raise HTTPException(
            status_code=503,
            detail="Cloudflare Access verification is unavailable",
        ) from exc
    except jwt.PyJWKClientConnectionError as exc:
        raise HTTPException(
            status_code=503,
            detail="Cloudflare Access verification is temporarily unavailable",
        ) from exc
    except jwt.PyJWKClientError as exc:
        raise HTTPException(
            status_code=401,
            detail="Cloudflare Access session is invalid or expired",
        ) from exc

    try:
        claims = jwt.decode(
            assertion,
            signing_key.key,
            algorithms=["RS256"],
            audience=audience,
            issuer=team_domain,
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=401,
            detail="Cloudflare Access session is invalid or expired",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    email = claims.get("email")
    if not isinstance(email, str) or not email.strip():
        raise HTTPException(
            status_code=401,
            detail="Cloudflare Access did not provide an operator email",
        )
    normalized_email = email.strip().casefold()
    principal = next(
        (
            candidate
            for candidate in settings.cloudflare_access_principals
            if normalized_email == candidate.email
        ),
        None,
    )
    if principal is None:
        raise HTTPException(
            status_code=403,
            detail=(
                "This Cloudflare Access email is not assigned to a "
                "Katcha operator"
            ),
        )

    _set_identity(
        request,
        actor=_principal_actor("cloudflare-access:" + principal.name),
        scopes=set(principal.scopes),
        channel_profile_ids=set(principal.channel_profile_ids),
        principal_name=principal.name,
        credential_id="cloudflare-access-identity",
        # Bound to the verified Cloudflare Access email, without persisting a JWT.
        credential_fingerprint=_credential_fingerprint(principal.email),
    )
    request.state.control_authentication_mode = "cloudflare_access"


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
    # This proof is an in-process Python object, never a request header.
    # It can be introduced only by GoalDelegatedApp for an exact native route.
    from katcha.api.goal_delegation import delegated_identity

    delegated = delegated_identity(request.scope)
    if delegated is not None:
        _set_identity(
            request,
            actor=delegated.actor,
            scopes=set(delegated.scopes),
            channel_profile_ids={delegated.channel_id},
            principal_name=delegated.principal_name,
            credential_id=delegated.credential_id,
            credential_fingerprint=delegated.credential_fingerprint,
        )
        request.state.control_authentication_mode = "goal_delegation"
        return

    if credentials is None and request.headers.get("Cf-Access-Jwt-Assertion"):
        _authenticate_cloudflare_access(request, settings)
        return

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
            if not settings.cloudflare_access_principals:
                raise HTTPException(
                    status_code=503,
                    detail="control-plane authentication not configured",
                )
            raise HTTPException(
                status_code=401,
                detail="control-plane authentication required",
                headers={"WWW-Authenticate": "Bearer"},
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

    if path.startswith("/v1/productions/") and path.endswith("/media") and method == "GET":
        # The endpoint resolves the production and enforces its channel before
        # reading any private media bytes.
        require_control_scope(request, "channels:read")
        return

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
        r"/v1/channels/[^/]+/editorial-projects/source-uploads"
        r"(?:/multipart(?:/[^/]+(?:/parts|/complete)?)?)?", path
    ):
        require_control_scope(
            request, "ai:read" if method in {"GET", "HEAD"} else "production:create"
        )
        return

    if method in {"GET", "HEAD"} and re.fullmatch(
        r"/v1/channels/[^/]+/editorial-projects/[^/]+/script-visual-plan", path
    ):
        # A script-derived inventory is read-only writing intent. It does not
        # authorize providers, media acquisition, rights decisions or rendering.
        require_control_scope(request, "ai:read")
        return

    if re.fullmatch(
        r"/v1/channels/[^/]+/editorial-projects/[^/]+/(?:narration|images)(?:/[^/]+/revoke)?", path
    ):
        require_control_scope(request, "ai:read" if method == "GET" else "production:create")
        return

    if re.fullmatch(
        r"/v1/channels/[^/]+/editorial-projects/[^/]+/storyboard"
        r"(?:/history|/undo)?",
        path,
    ):
        require_control_scope(
            request,
            "ai:read" if method in {"GET", "HEAD"} else "production:create",
        )
        return

    if method == "POST" and re.fullmatch(
        r"/v1/channels/[^/]+/editorial-projects/[^/]+/runs/[^/]+/preview-session",
        path,
    ):
        # Session is minted only to an authenticated, channel-authorized
        # editorial reader after current frozen-render clearance.
        require_control_scope(request, "ai:read")
        return

    if method == "POST" and re.fullmatch(
        r"/v1/(?:productions|clips)/[^/]+/media-session", path
    ):
        # Ticket minting still requires an authenticated channel-read principal;
        # each endpoint rechecks the particular resource before issuing.
        require_control_scope(request, "channels:read")
        return

    if method == "POST" and re.fullmatch(
        r"/v1/channels/[^/]+/editorial-projects/[^/]+/runs/[^/]+/assets/[^/]+/"
        r"source-media-session",
        path,
    ):
        # This only mints a short-lived, exact-path read capability. The media
        # route revalidates the signed scope and current source clearance.
        require_control_scope(request, "ai:read")
        return

    if method == "GET" and re.fullmatch(
        r"/v1/channels/[^/]+/editorial-projects/[^/]+/runs/[^/]+/assets/[^/]+/"
        r"(?:source-monitor|contact-sheet)",
        path,
    ):
        require_control_scope(request, "ai:read")
        return

    if method == "GET" and re.fullmatch(
        r"/v1/channels/[^/]+/editorial-projects/[^/]+/runs/[^/]+/frames/[0-9]+(?:/image)?",
        path,
    ):
        require_control_scope(request, "ai:read")
        return

    if method == "GET" and re.fullmatch(
        r"/v1/channels/[^/]+/editorial-projects/[^/]+/runs/[^/]+/(?:preview|program-map)",
        path,
    ):
        # Preview playback and its exact rendered-time map share Editorial read
        # scope; each router endpoint still checks channel, frozen revision and
        # current clearance before exposing review material.
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
    if (
        not request.url.path.startswith("/v1/")
        or request.url.path in _PUBLIC_PATHS
        or _uses_editorial_media_ticket(request)
        or _uses_review_media_ticket(request)
    ):
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
    if (
        request.method == "GET"
        and request.url.path.startswith("/v1/productions/")
        and request.url.path.endswith("/media")
    ):
        # This binary route is not an AI tool. Its endpoint checks the bound
        # production channel before accessing object storage.
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


def require_global_control_operator(request: Request) -> None:
    """Global credentials may only be managed by an unrestricted operator."""
    if "*" not in control_scopes(request) or control_allowed_channel_ids(request) is not None:
        raise HTTPException(403, "A global operator is required to manage shared AI connections")
