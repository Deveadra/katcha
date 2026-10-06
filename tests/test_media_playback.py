import uuid

from katcha.config import Settings
from katcha.services.media_playback import (
    issue_media_playback_grant,
    validate_media_playback_grant,
)


def settings() -> Settings:
    return Settings(
        _env_file=None,
        control_api_token="media-playback-test-token-0001",
    )


def test_media_playback_grant_is_clip_channel_scoped_and_expires() -> None:
    clip_id = uuid.uuid4()
    channel_id = uuid.uuid4()
    token, grant = issue_media_playback_grant(
        clip_id,
        channel_id,
        settings=settings(),
        now=1000,
        ttl_seconds=60,
    )

    assert grant.expires_at == 1060
    assert (
        validate_media_playback_grant(
            token,
            clip_id,
            channel_id,
            settings=settings(),
            now=1059,
        )
        == grant
    )
    assert (
        validate_media_playback_grant(
            token,
            uuid.uuid4(),
            channel_id,
            settings=settings(),
            now=1059,
        )
        is None
    )
    assert (
        validate_media_playback_grant(
            token,
            clip_id,
            uuid.uuid4(),
            settings=settings(),
            now=1059,
        )
        is None
    )
    assert (
        validate_media_playback_grant(
            token,
            clip_id,
            channel_id,
            settings=settings(),
            now=1060,
        )
        is None
    )


def test_media_playback_grant_rejects_tampering() -> None:
    clip_id = uuid.uuid4()
    channel_id = uuid.uuid4()
    token, _ = issue_media_playback_grant(
        clip_id,
        channel_id,
        settings=settings(),
        now=1000,
        ttl_seconds=60,
    )
    payload, signature = token.split(".", 1)
    replacement = "A" if signature[-1] != "A" else "B"
    tampered = payload + "." + signature[:-1] + replacement

    assert (
        validate_media_playback_grant(
            tampered,
            clip_id,
            channel_id,
            settings=settings(),
            now=1001,
        )
        is None
    )
