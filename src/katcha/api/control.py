from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from katcha import __version__
from katcha.api.control_auth import (
    control_actor,
    control_allowed_channel_ids,
    control_principal_name,
    control_scopes,
)

router = APIRouter(prefix="/v1/control", tags=["control-plane"])

CONTROL_CONTRACT_VERSION = "1"


class ChannelAccessResponse(BaseModel):
    all_channels: bool
    channel_profile_ids: list[uuid.UUID] = Field(default_factory=list)


class EventStreamCapabilityResponse(BaseModel):
    read_endpoint: str
    acknowledge_endpoint_template: str
    channel_scope_required: bool
    consumer_cursor_is_principal_bound: bool = True


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


class ControlSessionResponse(BaseModel):
    control_contract_version: str
    katcha_version: str
    actor: str
    principal_name: str | None
    authentication_mode: Literal[
        "named_principal",
        "legacy_token",
        "local_development",
    ]
    scopes: list[str]
    channel_access: ChannelAccessResponse
    capabilities: ControlCapabilitiesResponse
    event_stream: EventStreamCapabilityResponse


def _allows(scopes: set[str], scope: str) -> bool:
    return "*" in scopes or scope in scopes


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
            ai_read=_allows(scopes, "ai:read"),
            ai_command=_allows(scopes, "ai:command"),
            ai_write=_allows(scopes, "ai:write"),
            channels_read=_allows(scopes, "channels:read"),
            channels_write=_allows(scopes, "channels:write"),
            intelligence_write=_allows(scopes, "intelligence:write"),
            production_create=_allows(scopes, "production:create"),
            render_recover=_allows(scopes, "render:recover"),
            discovery_write=_allows(scopes, "discovery:write"),
            events_read=_allows(scopes, "events:read"),
            events_ack=_allows(scopes, "events:ack"),
            trends_read=_allows(scopes, "trends:read"),
            trends_write=_allows(scopes, "trends:write"),
        ),
        event_stream=EventStreamCapabilityResponse(
            read_endpoint="/v1/control/events",
            acknowledge_endpoint_template="/v1/control/events/{event_id}/ack",
            channel_scope_required=not all_channels,
        ),
    )
