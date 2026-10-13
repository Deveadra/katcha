from __future__ import annotations

import asyncio

import pytest

from katcha.api.media_export_response import MediaExportFileResponse


@pytest.mark.asyncio
@pytest.mark.parametrize("disconnect", [False, True])
async def test_archive_cleanup_on_completion_or_disconnect(tmp_path, disconnect):
    archive = tmp_path / "export.zip"
    archive.write_bytes(b"a" * (2 * 64 * 1024))
    body_sizes = []

    # Exercise actual AnyIO file I/O: replacing the thread runner globally
    # can deadlock newer Starlette/AnyIO file-response implementations.

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.body":
            body_sizes.append(len(message.get("body", b"")))
            if disconnect:
                raise OSError("client disconnected")

    response = MediaExportFileResponse(archive, filename="export.zip")
    scope = {"type": "http", "method": "GET", "headers": [], "extensions": {}}
    if disconnect:
        with pytest.raises(OSError, match="disconnected"):
            await asyncio.wait_for(response(scope, receive, send), timeout=10)
    else:
        await asyncio.wait_for(response(scope, receive, send), timeout=10)
        assert sum(body_sizes) == 2 * 64 * 1024
    assert body_sizes and max(body_sizes) <= 64 * 1024
    assert not archive.exists()
