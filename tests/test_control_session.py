import uuid
from datetime import UTC, datetime

from starlette.requests import Request

from katcha.api.control import CONTROL_CONTRACT_VERSION, control_session
from katcha.api.main import app


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/v1/control/session",
            "headers": [],
        }
    )


def test_control_session_reports_named_principal_capabilities_without_secrets() -> None:
    channel_id = uuid.uuid4()
    request = _request()
    request.state.control_actor = "control-principal:aerith"
    request.state.control_principal_name = "aerith"
    request.state.control_credential_id = "2026-q4"
    request.state.control_credential_fingerprint = "abc123def456"
    request.state.control_credential_not_before = datetime(
        2026, 9, 1, tzinfo=UTC
    )
    request.state.control_credential_expires_at = datetime(
        2026, 12, 1, tzinfo=UTC
    )
    request.state.control_scopes = {
        "channels:read",
        "events:read",
        "events:ack",
        "production:create",
    }
    request.state.control_channel_profile_ids = {str(channel_id)}

    response = control_session(request)
    payload = response.model_dump(mode="json")

    assert response.control_contract_version == CONTROL_CONTRACT_VERSION
    assert response.authentication_mode == "named_principal"
    assert response.actor == "control-principal:aerith"
    assert response.principal_name == "aerith"
    assert response.credential is not None
    assert response.credential.id == "2026-q4"
    assert response.credential.fingerprint == "abc123def456"
    assert response.credential.not_before == datetime(
        2026, 9, 1, tzinfo=UTC
    )
    assert response.credential.expires_at == datetime(
        2026, 12, 1, tzinfo=UTC
    )
    assert response.channel_access.all_channels is False
    assert response.channel_access.channel_profile_ids == [channel_id]
    assert response.capabilities.channels_read is True
    assert response.capabilities.events_read is True
    assert response.capabilities.events_ack is True
    assert response.capabilities.production_create is True
    assert response.capabilities.ai_command is False
    assert response.action_permissions["create_short_production"].allowed is True
    assert (
        response.action_permissions["create_short_production"].required_scope
        == "production:create"
    )
    assert response.action_permissions["recover_production_render"].allowed is False
    assert (
        response.action_permissions["recover_production_render"].required_scope
        == "render:recover"
    )
    assert response.event_stream.channel_scope_required is True
    assert "token" not in str(payload).casefold()
    assert "secret" not in str(payload).casefold()


def test_control_session_reports_wildcard_legacy_and_local_modes() -> None:
    legacy = _request()
    legacy.state.control_actor = "control-token:fixture"
    legacy.state.control_principal_name = None
    legacy.state.control_credential_id = "legacy-control-api-token"
    legacy.state.control_credential_fingerprint = "123456abcdef"
    legacy.state.control_scopes = {"*"}
    legacy.state.control_channel_profile_ids = {"*"}

    legacy_response = control_session(legacy)
    assert legacy_response.authentication_mode == "legacy_token"
    assert legacy_response.credential is not None
    assert legacy_response.credential.id == "legacy-control-api-token"
    assert legacy_response.credential.fingerprint == "123456abcdef"
    assert legacy_response.channel_access.all_channels is True
    assert legacy_response.capabilities.ai_command is True
    assert legacy_response.capabilities.trends_write is True
    assert all(
        permission.allowed
        for permission in legacy_response.action_permissions.values()
    )
    assert legacy_response.event_stream.channel_scope_required is False

    local = _request()
    local.state.control_actor = "local-development"
    local.state.control_principal_name = None
    local.state.control_scopes = {"*"}
    local.state.control_channel_profile_ids = {"*"}

    local_response = control_session(local)
    assert local_response.authentication_mode == "local_development"


def test_control_session_route_is_mounted() -> None:
    assert "/v1/control/session" in set(app.openapi()["paths"])
