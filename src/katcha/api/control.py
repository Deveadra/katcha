from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from katcha import __version__
from katcha.api.control_auth import (
    control_actor,
    control_allowed_channel_ids,
    control_credential_expires_at,
    control_credential_fingerprint,
    control_credential_id,
    control_credential_not_before,
    control_principal_name,
    control_scopes,
)
from katcha.control_contract import (
    CONTROL_CONTRACT_VERSION,
    capability_flags,
    command_action_permissions,
)

router = APIRouter(prefix="/v1/control", tags=["control-plane"])


class ChannelAccessResponse(BaseModel):
    all_channels: bool
    channel_profile_ids: list[uuid.UUID] = Field(default_factory=list)


class EventStreamCapabilityResponse(BaseModel):
    read_endpoint: str
    acknowledge_endpoint_template: str
    channel_scope_required: bool
    consumer_cursor_is_principal_bound: bool = True


class ControlCredentialResponse(BaseModel):
    id: str
    fingerprint: str
    not_before: datetime | None = None
    expires_at: datetime | None = None


class ControlCapabilitiesResponse(BaseModel):
    ai_read: bool
    ai_command: bool
    ai_write: bool
    channels_read: bool
    channels_write: bool
    intelligence_write: bool
    production_create: bool
    render_recover: bool
    discovery_write: bool
    events_read: bool
    events_ack: bool
    trends_read: bool
    trends_write: bool


class ActionPermissionResponse(BaseModel):
    required_scope: str
    allowed: bool


class ControlSessionResponse(BaseModel):
    control_contract_version: str
    katcha_version: str
    actor: str
    principal_name: str | None
    credential: ControlCredentialResponse | None = None
    authentication_mode: Literal[
        "named_principal",
        "legacy_token",
        "local_development",
    ]
    scopes: list[str]
    channel_access: ChannelAccessResponse
    capabilities: ControlCapabilitiesResponse
    action_permissions: dict[str, ActionPermissionResponse]
    event_stream: EventStreamCapabilityResponse


def _authentication_mode(
    *,
    actor: str,
    principal_name: str | None,
) -> Literal["named_principal", "legacy_token", "local_development"]:
    if principal_name is not None:
        return "named_principal"
    if actor == "local-development":
        return "local_development"
    return "legacy_token"


@router.get("/session", response_model=ControlSessionResponse)
def control_session(http_request: Request) -> ControlSessionResponse:
    actor = control_actor(http_request)
    principal_name = control_principal_name(http_request)
    credential_id = control_credential_id(http_request)
    credential_fingerprint = control_credential_fingerprint(http_request)
    credential = (
        ControlCredentialResponse(
            id=credential_id,
            fingerprint=credential_fingerprint,
            not_before=control_credential_not_before(http_request),
            expires_at=control_credential_expires_at(http_request),
        )
        if credential_id is not None and credential_fingerprint is not None
        else None
    )
    scopes = control_scopes(http_request)
    allowed_channels = control_allowed_channel_ids(http_request)
    all_channels = allowed_channels is None
    channel_ids = (
        []
        if all_channels
        else sorted(allowed_channels, key=str)
    )

    return ControlSessionResponse(
        control_contract_version=CONTROL_CONTRACT_VERSION,
        katcha_version=__version__,
        actor=actor,
        principal_name=principal_name,
        credential=credential,
        authentication_mode=_authentication_mode(
            actor=actor,
            principal_name=principal_name,
        ),
        scopes=sorted(scopes),
        channel_access=ChannelAccessResponse(
            all_channels=all_channels,
            channel_profile_ids=channel_ids,
        ),
        capabilities=ControlCapabilitiesResponse(
            **capability_flags(scopes),
        ),
        action_permissions={
            action_type: ActionPermissionResponse.model_validate(value)
            for action_type, value in command_action_permissions(scopes).items()
        },
        event_stream=EventStreamCapabilityResponse(
            read_endpoint="/v1/control/events",
            acknowledge_endpoint_template="/v1/control/events/{event_id}/ack",
            channel_scope_required=not all_channels,
        ),
    )
