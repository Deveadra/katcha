from katcha.services.event_stream import _safe_payload


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
