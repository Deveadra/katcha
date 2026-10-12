from __future__ import annotations

import anyio
import pytest

from katcha.api.media_export_response import MediaExportFileResponse


@pytest.mark.asyncio
@pytest.mark.parametrize("disconnect", [False, True])
async def test_archive_cleanup_on_completion_or_disconnect(tmp_path, monkeypatch, disconnect):
    archive = tmp_path / "export.zip"
    archive.write_bytes(b"a" * (2 * 64 * 1024))
    body_sizes = []

    async def run_sync(func, *args, **kwargs):
        return func(*args)

    # Keep file I/O deterministic; exercise the actual ASGI file response and send failure.
    monkeypatch.setattr(anyio.to_thread, "run_sync", run_sync)

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
            await response(scope, receive, send)
    else:
        await response(scope, receive, send)
        assert sum(body_sizes) == 2 * 64 * 1024
    assert body_sizes and max(body_sizes) <= 64 * 1024
    assert not archive.exists()
