from pathlib import Path

import pytest

from katcha.integrations.youtube.client import YouTubeAPIError, _next_offset, read_chunk


def test_resumable_range_to_next_offset() -> None:
    assert _next_offset(None) == 0
    assert _next_offset("bytes=0-1048575") == 1048576


def test_invalid_resumable_range_is_rejected() -> None:
    with pytest.raises(YouTubeAPIError):
        _next_offset("garbage")


def test_read_chunk_respects_offset(tmp_path: Path) -> None:
    path = tmp_path / "video.bin"
    path.write_bytes(b"abcdefghij")

    assert read_chunk(path, 3, 4) == b"defg"
