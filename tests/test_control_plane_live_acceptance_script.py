import uuid

import pytest

from scripts.control_plane_live_acceptance import (
    AcceptanceError,
    _correlated_events,
    _validate_distinct_sessions,
    _validate_session,
)


def _session(
    *,
    principal: str = "aerith",
    scopes: list[str] | None = None,
    all_channels: bool = False,
    channel_id: uuid.UUID | None = None,
    fingerprint: str = "aerith-fingerprint",
) -> dict[str, object]:
    return {
        "authentication_mode": "named_principal",
        "actor": f"control-principal:{principal}",
        "principal_name": principal,
        "credential": {
            "id": "current",
            "fingerprint": fingerprint,
        },
        "scopes": scopes
        or [
            "ai:read",
            "ai:command",
            "channels:read",
            "events:read",
            "events:ack",
            "intelligence:write",
        ],
        "channel_access": {
            "all_channels": all_channels,
            "channel_profile_ids": (
                [] if all_channels or channel_id is None else [str(channel_id)]
            ),
        },
    }


def test_aerith_acceptance_rejects_wildcard_channel_scope_by_default() -> None:
    channel_id = uuid.uuid4()
    payload = _session(all_channels=True, channel_id=channel_id)

    with pytest.raises(AcceptanceError, match="wildcard channel access"):
        _validate_session(
            payload,
            expected_principal="aerith",
            required_scopes={
                "ai:read",
                "ai:command",
                "events:read",
                "events:ack",
            },
            channel_profile_id=channel_id,
            allow_all_channels=False,
        )


def test_aerith_acceptance_rejects_missing_command_scope() -> None:
    channel_id = uuid.uuid4()
    payload = _session(
        scopes=[
            "ai:read",
            "channels:read",
            "events:read",
            "events:ack",
            "intelligence:write",
        ],
        channel_id=channel_id,
    )

    with pytest.raises(AcceptanceError, match="ai:command"):
        _validate_session(
            payload,
            expected_principal="aerith",
            required_scopes={"ai:read", "ai:command"},
            channel_profile_id=channel_id,
            allow_all_channels=False,
        )


def test_acceptance_rejects_shared_operator_and_aerith_credential() -> None:
    channel_id = uuid.uuid4()
    operator = _session(
        principal="operator-ui",
        all_channels=True,
        channel_id=channel_id,
        fingerprint="shared-fingerprint",
    )
    aerith = _session(
        principal="aerith",
        channel_id=channel_id,
        fingerprint="shared-fingerprint",
    )

    with pytest.raises(AcceptanceError, match="same bearer credential"):
        _validate_distinct_sessions(operator, aerith)


def test_correlated_events_only_returns_target_proposal() -> None:
    proposal_id = str(uuid.uuid4())
    other_id = str(uuid.uuid4())
    events = [
        {
            "id": str(uuid.uuid4()),
            "event_type": "command_center.proposal_created",
            "payload": {"proposal_id": proposal_id},
        },
        {
            "id": str(uuid.uuid4()),
            "event_type": "command_center.proposal_created",
            "payload": {"proposal_id": other_id},
        },
        {
            "id": str(uuid.uuid4()),
            "event_type": "production.render_started",
            "payload": {"channel_profile_id": str(uuid.uuid4())},
        },
    ]

    result = _correlated_events(events, proposal_id)

    assert len(result) == 1
    assert result[0]["payload"]["proposal_id"] == proposal_id
