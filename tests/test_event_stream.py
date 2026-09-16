import uuid

import pytest

from katcha.intelligence_models import EventConsumerCursor
from katcha.services.event_stream import _require_cursor_scope, _safe_payload


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
