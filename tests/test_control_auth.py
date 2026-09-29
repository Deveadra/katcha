import json
import uuid

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import ValidationError
from starlette.requests import Request

from katcha.api.command_center import CommandRequest, action_status, command
from katcha.api.control_auth import (
    _authenticate,
    _require_named_principal_route_access,
    control_actor,
    control_allowed_channel_ids,
    control_principal_name,
    require_control_channel,
    require_control_token,
)
from katcha.config import Settings


def _request(
    path: str = "/v1/channels",
    *,
    method: str = "GET",
    path_params: dict[str, str] | None = None,
) -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "raw_path": path.encode(),
            "headers": [],
            "query_string": b"",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 12345),
            "path_params": dict(path_params or {}),
        }
    )


def _credentials(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(
        scheme="Bearer",
        credentials=token,
    )


def _settings(
    *,
    channel_id: uuid.UUID | None = None,
    scopes: list[str] | None = None,
) -> Settings:
    return Settings(
        _env_file=None,
        control_principals=[
            {
                "name": "aerith",
                "token": "aerith-fixture-token-000001",
                "scopes": scopes or ["channels:read", "events:read"],
                "channel_profile_ids": [
                    str(channel_id) if channel_id is not None else "*"
                ],
            }
        ],
    )


def test_named_principal_auth_derives_identity_scopes_and_channels() -> None:
    channel_id = uuid.uuid4()
    request = _request()
    settings = _settings(channel_id=channel_id)

    _authenticate(
        request,
        _credentials("aerith-fixture-token-000001"),
        settings,
    )

    assert control_actor(request) == "control-principal:aerith"
    assert control_principal_name(request) == "aerith"
    assert request.state.control_scopes == {"channels:read", "events:read"}
    assert control_allowed_channel_ids(request) == {channel_id}

    require_control_channel(request, channel_id)
    with pytest.raises(HTTPException) as exc:
        require_control_channel(request, uuid.uuid4())
    assert exc.value.status_code == 403


def test_legacy_single_token_auth_remains_backward_compatible() -> None:
    request = _request()
    settings = Settings(
        _env_file=None,
        control_api_token="legacy-fixture-token-00001",
        control_api_scopes="ai:read,production:create",
    )

    _authenticate(
        request,
        _credentials("legacy-fixture-token-00001"),
        settings,
    )

    assert control_actor(request).startswith("control-token:")
    assert control_principal_name(request) is None
    assert request.state.control_scopes == {"ai:read", "production:create"}
    assert control_allowed_channel_ids(request) is None


def test_production_without_control_auth_fails_closed() -> None:
    request = _request()
    settings = Settings(_env_file=None, env="production")

    with pytest.raises(HTTPException) as exc:
        _authenticate(request, None, settings)

    assert exc.value.status_code == 503
    assert "not configured" in str(exc.value.detail)


def test_named_principal_rejects_unknown_bearer_token() -> None:
    request = _request()

    with pytest.raises(HTTPException) as exc:
        _authenticate(
            request,
            _credentials("wrong-fixture-token-000000"),
            _settings(),
        )

    assert exc.value.status_code == 401
    assert "authentication required" in str(exc.value.detail)


def test_named_principal_can_inspect_own_control_session_without_extra_scope() -> None:
    request = _request("/v1/control/session")
    settings = _settings(scopes=["events:read"])
    _authenticate(
        request,
        _credentials("aerith-fixture-token-000001"),
        settings,
    )

    _require_named_principal_route_access(request)


def test_named_principal_route_policy_fails_closed_for_unmapped_api() -> None:
    request = _request("/v1/clips")
    settings = _settings(scopes=["events:read"])
    _authenticate(
        request,
        _credentials("aerith-fixture-token-000001"),
        settings,
    )

    with pytest.raises(HTTPException) as exc:
        _require_named_principal_route_access(request)

    assert exc.value.status_code == 403
    assert "restricted control principals" in str(exc.value.detail)


def test_named_principal_ai_command_and_write_scopes_are_distinct() -> None:
    read_only = _settings(scopes=["ai:read"])

    command_request = _request("/v1/ai/command", method="POST")
    _authenticate(
        command_request,
        _credentials("aerith-fixture-token-000001"),
        read_only,
    )
    with pytest.raises(HTTPException) as exc:
        _require_named_principal_route_access(command_request)
    assert exc.value.status_code == 403
    assert "ai:command" in str(exc.value.detail)

    archive_request = _request(
        "/v1/ai/threads/00000000-0000-0000-0000-000000000000/archive",
        method="POST",
    )
    _authenticate(
        archive_request,
        _credentials("aerith-fixture-token-000001"),
        read_only,
    )
    with pytest.raises(HTTPException) as exc:
        _require_named_principal_route_access(archive_request)
    assert exc.value.status_code == 403
    assert "ai:write" in str(exc.value.detail)

    command_settings = _settings(scopes=["ai:command"])
    _authenticate(
        command_request,
        _credentials("aerith-fixture-token-000001"),
        command_settings,
    )
    _require_named_principal_route_access(command_request)


def test_named_principal_route_policy_requires_surface_scope() -> None:
    read_request = _request("/v1/channels")
    settings = _settings(scopes=["channels:read"])
    _authenticate(
        read_request,
        _credentials("aerith-fixture-token-000001"),
        settings,
    )
    _require_named_principal_route_access(read_request)

    write_request = _request("/v1/channels", method="POST")
    _authenticate(
        write_request,
        _credentials("aerith-fixture-token-000001"),
        settings,
    )
    with pytest.raises(HTTPException) as exc:
        _require_named_principal_route_access(write_request)
    assert exc.value.status_code == 403
    assert "channels:write" in str(exc.value.detail)


def test_wildcard_operator_principal_can_use_legacy_api_surfaces() -> None:
    request = _request("/v1/clips")
    settings = Settings(
        _env_file=None,
        control_principals=[
            {
                "name": "operator-ui",
                "token": "operator-fixture-token-0001",
                "scopes": ["*"],
                "channel_profile_ids": ["*"],
            }
        ],
    )
    _authenticate(
        request,
        _credentials("operator-fixture-token-0001"),
        settings,
    )

    _require_named_principal_route_access(request)


def test_control_principal_configuration_rejects_duplicates() -> None:
    with pytest.raises(ValidationError, match="duplicate control principal name"):
        Settings(
            _env_file=None,
            control_principals=[
                {
                    "name": "aerith",
                    "token": "aerith-fixture-token-000001",
                    "scopes": ["events:read"],
                    "channel_profile_ids": ["*"],
                },
                {
                    "name": "AERITH",
                    "token": "another-fixture-token-0001",
                    "scopes": ["events:read"],
                    "channel_profile_ids": ["*"],
                },
            ],
        )

    with pytest.raises(ValidationError, match="tokens must be unique"):
        Settings(
            _env_file=None,
            control_principals=[
                {
                    "name": "aerith",
                    "token": "shared-fixture-token-00001",
                    "scopes": ["events:read"],
                    "channel_profile_ids": ["*"],
                },
                {
                    "name": "operator",
                    "token": "shared-fixture-token-00001",
                    "scopes": ["*"],
                    "channel_profile_ids": ["*"],
                },
            ],
        )


def test_control_principal_configuration_rejects_short_tokens() -> None:
    with pytest.raises(ValidationError, match="at least 16 characters"):
        Settings(
            _env_file=None,
            control_principals=[
                {
                    "name": "aerith",
                    "token": "short",
                    "scopes": ["events:read"],
                    "channel_profile_ids": ["*"],
                }
            ],
        )


def _restricted_request(
    channel_id: uuid.UUID,
    *,
    scopes: set[str],
) -> Request:
    request = _request()
    request.state.control_actor = "control-principal:aerith"
    request.state.control_principal_name = "aerith"
    request.state.control_scopes = set(scopes)
    request.state.control_channel_profile_ids = {str(channel_id)}
    return request


def test_command_center_body_channel_cannot_escape_principal_allowlist() -> None:
    allowed = uuid.uuid4()
    blocked = uuid.uuid4()
    request = _restricted_request(allowed, scopes={"ai:read"})

    with pytest.raises(HTTPException) as exc:
        command(
            request,
            CommandRequest(
                channel_profile_id=blocked,
                prompt="What is currently failing?",
            ),
        )

    assert exc.value.status_code == 403
    assert "not authorized for this channel" in str(exc.value.detail)


def test_command_action_lookup_cannot_escape_principal_allowlist(
    monkeypatch,
) -> None:
    allowed = uuid.uuid4()
    blocked = uuid.uuid4()
    request = _restricted_request(allowed, scopes={"ai:read"})
    proposal_id = uuid.uuid4()

    class Proposal:
        id = proposal_id
        channel_profile_id = blocked

    monkeypatch.setattr(
        "katcha.api.command_center.get_action_proposal",
        lambda value: Proposal(),
    )

    with pytest.raises(HTTPException) as exc:
        action_status(proposal_id, request)

    assert exc.value.status_code == 403
    assert "not authorized for this channel" in str(exc.value.detail)


def test_control_principals_parse_from_environment_json(monkeypatch) -> None:
    raw_token = "environment-fixture-token-0001"
    monkeypatch.setenv(
        "KATCHA_CONTROL_PRINCIPALS",
        json.dumps(
            [
                {
                    "name": "operator-ui",
                    "token": raw_token,
                    "scopes": ["*"],
                    "channel_profile_ids": ["*"],
                }
            ]
        ),
    )

    settings = Settings(_env_file=None)

    assert settings.control_principals[0].name == "operator-ui"
    assert settings.control_principals[0].token.get_secret_value() == raw_token
    assert raw_token not in repr(settings.control_principals[0])


def test_global_control_dependency_enforces_channel_allowlist(
    monkeypatch,
) -> None:
    allowed = uuid.uuid4()
    blocked = uuid.uuid4()
    settings = _settings(
        channel_id=allowed,
        scopes=["channels:read"],
    )
    monkeypatch.setattr(
        "katcha.api.control_auth.get_settings",
        lambda: settings,
    )
    request = _request(
        f"/v1/channels/{blocked}",
        path_params={"channel_profile_id": str(blocked)},
    )

    with pytest.raises(HTTPException) as exc:
        require_control_token(
            request,
            _credentials("aerith-fixture-token-000001"),
        )

    assert exc.value.status_code == 403


def test_global_control_dependency_denies_unmapped_route(
    monkeypatch,
) -> None:
    settings = _settings(scopes=["events:read"])
    monkeypatch.setattr(
        "katcha.api.control_auth.get_settings",
        lambda: settings,
    )
    request = _request("/v1/clips")

    with pytest.raises(HTTPException) as exc:
        require_control_token(
            request,
            _credentials("aerith-fixture-token-000001"),
        )

    assert exc.value.status_code == 403
