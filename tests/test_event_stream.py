import uuid

import pytest

from katcha.intelligence_models import EventConsumerCursor
from katcha.services.event_stream import (
    _belongs_to_channel,
    _require_cursor_principal,
    _require_cursor_scope,
    _safe_payload,
)
from katcha.services.render_recovery import _event


def test_event_payload_scrubs_secret_bearing_keys_recursively() -> None:
    payload = {
        "channel_profile_id": "safe",
        "access_token": "secret",
        "nested": {
            "encrypted_upload_url": "secret",
            "workflow_id": "safe-workflow",
            "items": [
                {"client_secret": "secret", "name": "safe"},
            ],
        },
    }

    result = _safe_payload(payload)

    assert result == {
        "channel_profile_id": "safe",
        "nested": {
            "workflow_id": "safe-workflow",
            "items": [{"name": "safe"}],
        },
    }


def test_event_cursor_cannot_cross_channel_scope() -> None:
    channel_a = uuid.uuid4()
    channel_b = uuid.uuid4()
    cursor = EventConsumerCursor(
        consumer_key="aerith-channel-a",
        cursor_metadata={"_channel_profile_id": str(channel_a)},
    )

    _require_cursor_scope(cursor, channel_a)
    with pytest.raises(ValueError, match="different channel scope"):
        _require_cursor_scope(cursor, channel_b)
    with pytest.raises(ValueError, match="different channel scope"):
        _require_cursor_scope(cursor, None)


def test_unscoped_event_cursor_stays_unscoped() -> None:
    cursor = EventConsumerCursor(
        consumer_key="aerith-global",
        cursor_metadata={"_channel_profile_id": None},
    )

    _require_cursor_scope(cursor, None)



def test_render_events_remain_visible_in_channel_scoped_streams() -> None:
    channel_id = uuid.uuid4()
    production_id = uuid.uuid4()
    event = _event(
        source_kind="production",
        source_id=production_id,
        channel_profile_id=channel_id,
        event_type="production.render_attempt_verified",
        payload={"render_attempt_id": str(uuid.uuid4())},
    )

    assert _belongs_to_channel(event, channel_id) is True
    assert event.payload["channel_profile_id"] == str(channel_id)



def test_event_cursor_cannot_cross_control_principal() -> None:
    cursor = EventConsumerCursor(
        consumer_key="aerith-channel-a",
        cursor_metadata={
            "_channel_profile_id": None,
            "_control_actor": "control-principal:aerith",
        },
    )

    _require_cursor_principal(cursor, "control-principal:aerith")
    with pytest.raises(ValueError, match="different control principal"):
        _require_cursor_principal(cursor, "control-principal:operator")
