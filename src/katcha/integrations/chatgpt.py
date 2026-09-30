from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx
from sqlalchemy import select

from katcha.chatgpt_models import ChatGPTConnection, ChatGPTOAuthState
from katcha.config import Settings, get_settings
from katcha.db import session_scope
from katcha.security.secrets import decrypt_secret, encrypt_secret

AUTHORIZATION_URL = "https://auth.openai.com/api/accounts/authorize"
TOKEN_URL = "https://auth.openai.com/api/accounts/oauth/token"
OIDC_CONFIGURATION_URL = "https://auth.openai.com/.well-known/openid-configuration"
JWKS_URL = "https://auth.openai.com/.well-known/jwks.json"
ISSUER = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
MODELS_URL = "https://api.openai.com/v1/models"
DYNAMIC_CLIENT_ID = "dynamic_agent_client"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
SCOPES = (
    "openid",
    "profile",
    "email",
    "offline_access",
    "resource.invoke",
    PLAN_SCOPE,
)

_refresh_lock = threading.Lock()


class ChatGPTConnectionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ChatGPTModel:
    slug: str
    display_name: str


@dataclass(frozen=True, slots=True)
class ChatGPTSession:
    connection_id: uuid.UUID
    access_token: str
    model: str
    email: str | None
    display_name: str | None


@dataclass(frozen=True, slots=True)
class ChatGPTInference:
    text: str
    model: str
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class ChatGPTWebSearchInference:
    payload: dict[str, object]
    model: str
    input_tokens: int
    output_tokens: int


def _state_hash(state: str) -> str:
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _normalize_scopes(raw: object) -> list[str]:
    if isinstance(raw, str):
        values = raw.replace(",", " ").split()
    elif isinstance(raw, list):
        values = [str(value) for value in raw]
    else:
        values = []
    return list(dict.fromkeys(value.strip() for value in values if value.strip()))


def _require_settings(settings: Settings) -> None:
    if not settings.credential_encryption_key:
        raise ChatGPTConnectionError(
            "Credential encryption is not configured. Start Katcha through the launcher once."
        )
    if not settings.chatgpt_host_id:
        raise ChatGPTConnectionError(
            "ChatGPT host identity is not configured. Restart the Katcha launcher."
        )


def authorization_parameters(
    *,
    settings: Settings,
    state: str,
    nonce: str,
    code_challenge: str,
    issued_client_id: str | None = None,
    id_token_hint: str | None = None,
    login_hint: str | None = None,
) -> dict[str, str]:
    client_id = issued_client_id or DYNAMIC_CLIENT_ID
    params = {
        "client_id": client_id,
        "ext_agent_host_id": settings.chatgpt_host_id or "",
        "response_type": "code",
        "redirect_uri": settings.chatgpt_oauth_redirect_uri,
        "scope": " ".join(SCOPES),
        "resource": RESOURCE,
        "state": state,
        "nonce": nonce,
        "code_challenge_method": "S256",
        "code_challenge": code_challenge,
    }
    if issued_client_id is None:
        params["agent_name_hint"] = "Katcha"
    else:
        if id_token_hint:
            params["id_token_hint"] = id_token_hint
        if login_hint:
            params["login_hint"] = login_hint
    return params


def _authorization_url(**kwargs: object) -> str:
    return AUTHORIZATION_URL + "?" + urlencode(kwargs)


def active_connection() -> ChatGPTConnection | None:
    with session_scope() as session:
        row = session.scalar(
            select(ChatGPTConnection)
            .where(ChatGPTConnection.active.is_(True))
            .order_by(ChatGPTConnection.updated_at.desc())
            .limit(1)
        )
        if row is not None:
            session.expunge(row)
        return row


def begin_chatgpt_oauth(
    *,
    connection_id: uuid.UUID | None = None,
    settings: Settings | None = None,
) -> str:
    settings = settings or get_settings()
    _require_settings(settings)

    issued_client_id: str | None = None
    id_token_hint: str | None = None
    login_hint: str | None = None
    if connection_id is not None:
        with session_scope() as session:
            connection = session.get(ChatGPTConnection, connection_id)
            if connection is None:
                raise ChatGPTConnectionError("Saved ChatGPT connection was not found")
            issued_client_id = connection.issued_client_id
            login_hint = connection.email
            if connection.encrypted_id_token:
                id_token_hint = decrypt_secret(connection.encrypted_id_token, settings)

    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    now = datetime.now(UTC)
    with session_scope() as session:
        session.add(
            ChatGPTOAuthState(
                state_hash=_state_hash(state),
                nonce=nonce,
                encrypted_code_verifier=encrypt_secret(verifier, settings),
                redirect_uri=settings.chatgpt_oauth_redirect_uri,
                expected_client_id=issued_client_id,
                connection_id=connection_id,
                expires_at=now + timedelta(minutes=10),
            )
        )

    params = authorization_parameters(
        settings=settings,
        state=state,
        nonce=nonce,
        code_challenge=_pkce_challenge(verifier),
        issued_client_id=issued_client_id,
        id_token_hint=id_token_hint,
        login_hint=login_hint,
    )
    return _authorization_url(**params)


def consume_failed_oauth(state: str) -> None:
    """Validate and consume an OAuth attempt that returned an authorization error."""
    now = datetime.now(UTC)
    with session_scope() as session:
        row = session.scalar(
            select(ChatGPTOAuthState)
            .where(ChatGPTOAuthState.state_hash == _state_hash(state))
            .with_for_update()
        )
        if row is None:
            raise ChatGPTConnectionError("ChatGPT OAuth state is unknown")
        if row.consumed_at is not None:
            raise ChatGPTConnectionError("ChatGPT OAuth state has already been consumed")
        if row.expires_at <= now:
            raise ChatGPTConnectionError("ChatGPT OAuth state has expired")
        row.consumed_at = now


def _token_exchange(
    *,
    client_id: str,
    code: str,
    verifier: str,
    redirect_uri: str,
) -> dict[str, object]:
    response = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
            "resource": RESOURCE,
        },
        timeout=30.0,
    )
    if response.is_error:
        detail = response.text[:600]
        raise ChatGPTConnectionError(
            f"ChatGPT authorization exchange failed (HTTP {response.status_code}): {detail}"
        )
    payload = response.json()
    if not payload.get("access_token") or not payload.get("refresh_token"):
        raise ChatGPTConnectionError(
            "ChatGPT authorization did not return renewable credentials"
        )
    return payload


def _validate_id_token(
    id_token: str,
    *,
    client_id: str,
    nonce: str,
) -> dict[str, object]:
    import jwt

    try:
        header = jwt.get_unverified_header(id_token)
        algorithm = str(header.get("alg") or "")
        if algorithm not in {"RS256", "ES256"}:
            raise ChatGPTConnectionError(
                f"Unsupported ChatGPT ID-token algorithm: {algorithm or 'missing'}"
            )
        signing_key = jwt.PyJWKClient(JWKS_URL).get_signing_key_from_jwt(id_token).key
        claims = jwt.decode(
            id_token,
            signing_key,
            algorithms=[algorithm],
            audience=client_id,
            issuer=ISSUER,
            options={"require": ["exp", "iss", "sub", "aud"]},
        )
    except ChatGPTConnectionError:
        raise
    except Exception as exc:
        raise ChatGPTConnectionError(
            f"ChatGPT identity validation failed: {type(exc).__name__}"
        ) from exc

    if not secrets.compare_digest(str(claims.get("nonce") or ""), nonce):
        raise ChatGPTConnectionError("ChatGPT identity nonce did not match this sign-in")
    return {str(key): value for key, value in claims.items()}


def _models_for_token(access_token: str) -> list[ChatGPTModel]:
    response = httpx.get(
        MODELS_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=20.0,
    )
    if response.is_error:
        raise ChatGPTConnectionError(
            f"ChatGPT model catalog failed (HTTP {response.status_code})"
        )
    payload = response.json()
    raw_models = payload.get("models") or payload.get("data") or []
    result: list[ChatGPTModel] = []
    for raw in raw_models:
        if not isinstance(raw, dict):
            continue
        if raw.get("visibility") not in {None, "list"}:
            continue
        slug = str(raw.get("slug") or raw.get("id") or "").strip()
        if not slug:
            continue
        result.append(
            ChatGPTModel(
                slug=slug,
                display_name=str(raw.get("display_name") or slug),
            )
        )
    if not result:
        raise ChatGPTConnectionError(
            "ChatGPT returned no models available for this account"
        )
    return result


def complete_chatgpt_oauth(
    *,
    state: str,
    code: str,
    returned_client_id: str | None,
    settings: Settings | None = None,
) -> ChatGPTConnection:
    settings = settings or get_settings()
    _require_settings(settings)
    now = datetime.now(UTC)

    with session_scope() as session:
        oauth_state = session.scalar(
            select(ChatGPTOAuthState).where(
                ChatGPTOAuthState.state_hash == _state_hash(state)
            )
        )
        if oauth_state is None:
            raise ChatGPTConnectionError("ChatGPT OAuth state is unknown")
        if oauth_state.consumed_at is not None:
            raise ChatGPTConnectionError("ChatGPT OAuth state has already been consumed")
        if oauth_state.expires_at <= now:
            raise ChatGPTConnectionError("ChatGPT OAuth state has expired")
        state_id = oauth_state.id
        nonce = oauth_state.nonce
        redirect_uri = oauth_state.redirect_uri
        expected_client_id = oauth_state.expected_client_id
        connection_id = oauth_state.connection_id
        verifier = decrypt_secret(oauth_state.encrypted_code_verifier, settings)

    if expected_client_id is None:
        if not returned_client_id or returned_client_id == DYNAMIC_CLIENT_ID:
            raise ChatGPTConnectionError(
                "ChatGPT registration did not return an issued client ID"
            )
        client_id = returned_client_id
    else:
        client_id = expected_client_id
        if returned_client_id and returned_client_id != expected_client_id:
            raise ChatGPTConnectionError(
                "ChatGPT returned a different client registration than the selected account"
            )

    token = _token_exchange(
        client_id=client_id,
        code=code,
        verifier=verifier,
        redirect_uri=redirect_uri,
    )
    scopes = _normalize_scopes(token.get("scope"))
    if PLAN_SCOPE not in scopes:
        raise ChatGPTConnectionError(
            "ChatGPT sign-in succeeded, but plan usage was not granted. "
            "Authorize ChatGPT plan usage to use Katcha without an API key."
        )
    id_token = str(token.get("id_token") or "")
    if not id_token:
        raise ChatGPTConnectionError("ChatGPT authorization did not return an ID token")
    claims = _validate_id_token(id_token, client_id=client_id, nonce=nonce)
    subject = str(claims["sub"])
    email = str(claims.get("email") or "").strip() or None
    display_name = str(
        claims.get("name") or claims.get("preferred_username") or ""
    ).strip() or None
    access_token = str(token["access_token"])
    refresh_token = str(token["refresh_token"])
    expires_in = max(60, int(token.get("expires_in") or 3600))
    try:
        models = _models_for_token(access_token)
    except ChatGPTConnectionError:
        # Authentication succeeded. Model discovery is retried from Settings/readiness
        # so a temporary catalog outage does not discard valid OAuth credentials.
        models = []

    with session_scope() as session:
        locked_state = session.get(ChatGPTOAuthState, state_id)
        if locked_state is None or locked_state.consumed_at is not None:
            raise ChatGPTConnectionError("ChatGPT OAuth state was consumed concurrently")
        locked_state.consumed_at = datetime.now(UTC)

        connection = (
            session.get(ChatGPTConnection, connection_id)
            if connection_id is not None
            else session.scalar(
                select(ChatGPTConnection).where(
                    ChatGPTConnection.issued_client_id == client_id
                )
            )
        )
        if connection is not None and connection.subject != subject:
            raise ChatGPTConnectionError(
                "ChatGPT account identity did not match the saved registration"
            )
        if connection is None:
            connection = ChatGPTConnection(
                issued_client_id=client_id,
                issuer=ISSUER,
                subject=subject,
            )
            session.add(connection)

        for row in session.scalars(
            select(ChatGPTConnection).where(ChatGPTConnection.active.is_(True))
        ):
            row.active = False

        available = {model.slug for model in models}
        selected = (
            connection.selected_model
            if connection.selected_model and (
                not available or connection.selected_model in available
            )
            else None
        )
        connection.email = email
        connection.display_name = display_name
        connection.scopes = scopes
        connection.encrypted_access_token = encrypt_secret(access_token, settings)
        connection.encrypted_refresh_token = encrypt_secret(refresh_token, settings)
        connection.encrypted_id_token = encrypt_secret(id_token, settings)
        connection.access_token_expires_at = now + timedelta(seconds=expires_in)
        connection.selected_model = selected or (models[0].slug if models else None)
        connection.active = True
        connection.connection_metadata = {
            "plan_usage": True,
            "model_count": len(models),
            "model_catalog_pending": not bool(models),
        }
        session.flush()
        session.refresh(connection)
        session.expunge(connection)
        return connection


def _refresh(connection_id: uuid.UUID, settings: Settings) -> ChatGPTConnection:
    with _refresh_lock:
        with session_scope() as session:
            connection = session.get(ChatGPTConnection, connection_id)
            if connection is None or not connection.active:
                raise ChatGPTConnectionError("ChatGPT connection is no longer active")
            if (
                connection.access_token_expires_at is not None
                and connection.encrypted_access_token
                and connection.access_token_expires_at
                > datetime.now(UTC) + timedelta(minutes=2)
            ):
                session.expunge(connection)
                return connection
            if not connection.encrypted_refresh_token:
                raise ChatGPTConnectionError(
                    "ChatGPT renewable credentials are missing; reconnect the saved account"
                )
            client_id = connection.issued_client_id
            refresh_token = decrypt_secret(connection.encrypted_refresh_token, settings)

        response = httpx.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": refresh_token,
                "resource": RESOURCE,
            },
            timeout=30.0,
        )
        if response.is_error:
            raise ChatGPTConnectionError(
                f"ChatGPT session refresh failed (HTTP {response.status_code})"
            )
        payload = response.json()
        access_token = str(payload.get("access_token") or "")
        if not access_token:
            raise ChatGPTConnectionError("ChatGPT session refresh returned no access token")
        replacement_refresh = str(payload.get("refresh_token") or refresh_token)
        expires_in = max(60, int(payload.get("expires_in") or 3600))
        scopes = _normalize_scopes(payload.get("scope")) or None
        if scopes is not None and PLAN_SCOPE not in scopes:
            raise ChatGPTConnectionError("ChatGPT plan usage permission is no longer active")

        with session_scope() as session:
            connection = session.get(ChatGPTConnection, connection_id)
            if connection is None or not connection.active:
                raise ChatGPTConnectionError("ChatGPT connection is no longer active")
            connection.encrypted_access_token = encrypt_secret(access_token, settings)
            connection.encrypted_refresh_token = encrypt_secret(
                replacement_refresh, settings
            )
            connection.access_token_expires_at = datetime.now(UTC) + timedelta(
                seconds=expires_in
            )
            if scopes is not None:
                connection.scopes = scopes
            session.flush()
            session.refresh(connection)
            session.expunge(connection)
            return connection


def active_session(settings: Settings | None = None) -> ChatGPTSession | None:
    settings = settings or get_settings()
    connection = active_connection()
    if connection is None:
        return None
    if PLAN_SCOPE not in set(connection.scopes or []):
        return None
    if (
        connection.access_token_expires_at is None
        or not connection.encrypted_access_token
        or connection.access_token_expires_at
        <= datetime.now(UTC) + timedelta(minutes=2)
    ):
        connection = _refresh(connection.id, settings)
    if not connection.selected_model:
        access = decrypt_secret(connection.encrypted_access_token, settings)
        models = _models_for_token(access)
        selected_model = models[0].slug
        with session_scope() as db:
            row = db.get(ChatGPTConnection, connection.id)
            if row is None or not row.active:
                return None
            row.selected_model = selected_model
            db.flush()
            db.refresh(row)
            db.expunge(row)
            connection = row
    if not connection.encrypted_access_token:
        raise ChatGPTConnectionError(
            "ChatGPT access credentials are missing; reconnect the saved account"
        )
    return ChatGPTSession(
        connection_id=connection.id,
        access_token=decrypt_secret(connection.encrypted_access_token, settings),
        model=str(connection.selected_model),
        email=connection.email,
        display_name=connection.display_name,
    )


def list_models(settings: Settings | None = None) -> list[ChatGPTModel]:
    session = active_session(settings)
    if session is None:
        raise ChatGPTConnectionError("No ChatGPT account is connected")
    return _models_for_token(session.access_token)


def set_selected_model(model: str, settings: Settings | None = None) -> ChatGPTConnection:
    settings = settings or get_settings()
    session_info = active_session(settings)
    if session_info is None:
        raise ChatGPTConnectionError("No ChatGPT account is connected")
    models = _models_for_token(session_info.access_token)
    available = {item.slug for item in models}
    if model not in available:
        raise ChatGPTConnectionError(
            f"Model {model!r} is not available to the connected ChatGPT account"
        )
    with session_scope() as session:
        connection = session.get(ChatGPTConnection, session_info.connection_id)
        if connection is None:
            raise ChatGPTConnectionError("ChatGPT connection was not found")
        connection.selected_model = model
        session.flush()
        session.refresh(connection)
        session.expunge(connection)
        return connection


def connection_status() -> dict[str, object]:
    connection = active_connection()
    if connection is not None:
        return {
            "connected": True,
            "plan_usage_enabled": PLAN_SCOPE in set(connection.scopes or []),
            "provider": "chatgpt",
            "connection_id": str(connection.id),
            "saved_connection_id": str(connection.id),
            "email": connection.email,
            "display_name": connection.display_name,
            "selected_model": connection.selected_model,
            "access_token_expires_at": connection.access_token_expires_at,
        }

    with session_scope() as session:
        saved = session.scalar(
            select(ChatGPTConnection)
            .order_by(ChatGPTConnection.updated_at.desc())
            .limit(1)
        )
        if saved is not None:
            session.expunge(saved)
    if saved is None:
        return {
            "connected": False,
            "plan_usage_enabled": False,
            "provider": "chatgpt",
        }
    return {
        "connected": False,
        "plan_usage_enabled": False,
        "provider": "chatgpt",
        "saved_connection_id": str(saved.id),
        "email": saved.email,
        "display_name": saved.display_name,
        "selected_model": saved.selected_model,
    }


def _stream_plan_response(
    session: ChatGPTSession,
    *,
    body: dict[str, object],
    timeout: float,
) -> tuple[str, dict[str, object] | None, int, int]:
    payload = {
        "model": session.model,
        "store": False,
        "stream": True,
        **body,
    }
    chunks: list[str] = []
    completed_payload: dict[str, object] | None = None
    input_tokens = 0
    output_tokens = 0
    completed = False

    try:
        with httpx.stream(
            "POST",
            "https://api.openai.com/v1/responses",
            headers={
                "Authorization": "Bearer " + session.access_token,
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
            json=payload,
            timeout=timeout,
        ) as response:
            if response.status_code >= 400:
                raw = response.read().decode("utf-8", errors="replace")
                request_id = response.headers.get("x-request-id") or response.headers.get(
                    "openai-request-id"
                )
                try:
                    parsed = json.loads(raw)
                except ValueError:
                    parsed = None
                detail = ""
                if isinstance(parsed, dict):
                    error = parsed.get("error")
                    if isinstance(error, dict):
                        detail = ": ".join(
                            value
                            for value in (
                                str(error.get("code") or "").strip(),
                                str(error.get("message") or "").strip(),
                            )
                            if value
                        )
                    if not detail:
                        detail = str(
                            parsed.get("detail") or parsed.get("message") or ""
                        ).strip()
                if not detail:
                    detail = " ".join(raw.split())[:700]
                request_suffix = f"; request_id={request_id}" if request_id else ""
                raise ChatGPTConnectionError(
                    f"ChatGPT plan request rejected (HTTP {response.status_code}"
                    f"{request_suffix})" + (f": {detail}" if detail else "")
                )

            data_lines: list[str] = []
            for line in response.iter_lines():
                if line == "":
                    if not data_lines:
                        continue
                    raw_event = "\n".join(data_lines)
                    data_lines.clear()
                    if raw_event == "[DONE]":
                        continue
                    try:
                        event = json.loads(raw_event)
                    except ValueError as exc:
                        raise ChatGPTConnectionError(
                            "ChatGPT plan returned an invalid streaming event"
                        ) from exc
                    if not isinstance(event, dict):
                        continue
                    event_type = str(event.get("type") or "")
                    if event_type == "response.output_text.delta":
                        chunks.append(str(event.get("delta") or ""))
                    elif event_type == "response.failed":
                        response_payload = event.get("response")
                        error = (
                            response_payload.get("error")
                            if isinstance(response_payload, dict)
                            else None
                        )
                        code = (
                            str(error.get("code") or "unknown_error")
                            if isinstance(error, dict)
                            else "unknown_error"
                        )
                        message = (
                            str(error.get("message") or "").strip()
                            if isinstance(error, dict)
                            else ""
                        )
                        raise ChatGPTConnectionError(
                            f"ChatGPT plan inference failed: {code}"
                            + (f": {message}" if message else "")
                        )
                    elif event_type == "response.incomplete":
                        raise ChatGPTConnectionError(
                            "ChatGPT plan inference ended incomplete"
                        )
                    elif event_type == "response.completed":
                        completed = True
                        response_payload = event.get("response")
                        if isinstance(response_payload, dict):
                            completed_payload = dict(response_payload)
                            usage = response_payload.get("usage")
                            if isinstance(usage, dict):
                                input_tokens = int(usage.get("input_tokens") or 0)
                                output_tokens = int(usage.get("output_tokens") or 0)
                    continue
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
    except ChatGPTConnectionError:
        raise
    except httpx.HTTPError as exc:
        raise ChatGPTConnectionError(
            f"ChatGPT plan transport failed: {type(exc).__name__}: {exc}"
        ) from exc

    if not completed:
        raise ChatGPTConnectionError(
            "ChatGPT plan stream ended before response.completed"
        )
    return "".join(chunks).strip(), completed_payload, input_tokens, output_tokens


def test_connection(settings: Settings | None = None) -> dict[str, object]:
    session = active_session(settings)
    if session is None:
        raise ChatGPTConnectionError("No ChatGPT account is connected")
    models = _models_for_token(session.access_token)
    text, _, _, _ = _stream_plan_response(
        session,
        body={
            "input": [
                {
                    "role": "user",
                    "content": "Reply with exactly: KATCHA_CONNECTED",
                }
            ]
        },
        timeout=30.0,
    )
    if "KATCHA_CONNECTED" not in text:
        raise ChatGPTConnectionError(
            "ChatGPT model catalog is reachable, but live inference returned "
            "an unexpected response"
        )
    return {
        "ok": True,
        "model_count": len(models),
        "selected_model": session.model,
        "display_name": session.display_name,
        "email": session.email,
        "inference_verified": True,
    }


def disconnect(settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    connection = active_connection()
    if connection is None:
        return False
    refresh_token = (
        decrypt_secret(connection.encrypted_refresh_token, settings)
        if connection.encrypted_refresh_token
        else None
    )
    try:
        discovery = httpx.get(OIDC_CONFIGURATION_URL, timeout=10.0)
        discovery.raise_for_status()
        revocation_endpoint = str(discovery.json().get("revocation_endpoint") or "")
        if revocation_endpoint and refresh_token:
            response = httpx.post(
                revocation_endpoint,
                data={
                    "token": refresh_token,
                    "token_type_hint": "refresh_token",
                    "client_id": connection.issued_client_id,
                },
                timeout=15.0,
            )
            response.raise_for_status()
    except Exception:
        # Local sign-out must still complete. The user can revoke remote access
        # from ChatGPT Settings if the network was unavailable.
        pass

    with session_scope() as session:
        row = session.get(ChatGPTConnection, connection.id)
        if row is not None:
            row.active = False
            row.encrypted_access_token = None
            row.encrypted_refresh_token = None
            row.encrypted_id_token = None
            row.access_token_expires_at = None
    return True


def invoke_json(
    *,
    prompt: str,
    schema_name: str,
    schema: dict[str, object],
    settings: Settings | None = None,
) -> ChatGPTInference:
    session = active_session(settings)
    if session is None:
        raise ChatGPTConnectionError("No ChatGPT plan connection is available")

    text, _, input_tokens, output_tokens = _stream_plan_response(
        session,
        body={
            "input": [{"role": "user", "content": prompt}],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "schema": schema,
                    "strict": False,
                }
            },
        },
        timeout=45.0,
    )
    if not text:
        raise ChatGPTConnectionError("ChatGPT plan response contained no text")
    return ChatGPTInference(
        text=text,
        model=session.model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def invoke_web_search_json(
    *,
    prompt: str,
    schema_name: str,
    schema: dict[str, object],
    tool: dict[str, object],
    settings: Settings | None = None,
) -> ChatGPTWebSearchInference:
    """Run one grounded web-search turn against the connected ChatGPT plan."""
    session = active_session(settings)
    if session is None:
        raise ChatGPTConnectionError("No ChatGPT plan connection is available")

    from openai import OpenAI

    client = OpenAI(
        api_key=session.access_token,
        base_url="https://api.openai.com/v1",
        timeout=90.0,
        max_retries=0,
    )
    completed_payload: dict[str, object] | None = None
    input_tokens = 0
    output_tokens = 0
    try:
        with client.responses.create(
            model=session.model,
            input=[{"role": "user", "content": prompt}],
            store=False,
            stream=True,
            reasoning={"effort": "low"},
            tools=[tool],
            tool_choice="required",
            include=["web_search_call.action.sources"],
            text={
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "schema": schema,
                    "strict": False,
                }
            },
        ) as stream:
            for event in stream:
                event_type = str(getattr(event, "type", ""))
                if event_type == "response.failed":
                    response = getattr(event, "response", None)
                    error = getattr(response, "error", None)
                    code = getattr(error, "code", None) or "unknown_error"
                    raise ChatGPTConnectionError(
                        f"ChatGPT plan web search failed: {code}"
                    )
                if event_type == "response.incomplete":
                    raise ChatGPTConnectionError(
                        "ChatGPT plan web search ended incomplete"
                    )
                if event_type != "response.completed":
                    continue
                response = getattr(event, "response", None)
                usage = getattr(response, "usage", None)
                input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
                output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
                if response is not None and hasattr(response, "model_dump"):
                    raw = response.model_dump(mode="json", exclude_none=True)
                    if isinstance(raw, dict):
                        completed_payload = {
                            str(key): value for key, value in raw.items()
                        }
    except ChatGPTConnectionError:
        raise
    except Exception as exc:
        raise ChatGPTConnectionError(
            f"ChatGPT plan web search failed: {type(exc).__name__}: {exc}"
        ) from exc

    if completed_payload is None:
        raise ChatGPTConnectionError(
            "ChatGPT plan web search ended without a completed response payload"
        )
    return ChatGPTWebSearchInference(
        payload=completed_payload,
        model=session.model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
