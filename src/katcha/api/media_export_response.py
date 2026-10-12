"""Serve a temporary project archive without retaining it after a disconnected response."""

from pathlib import Path

from starlette.responses import FileResponse
from starlette.types import Receive, Scope, Send


class MediaExportFileResponse(FileResponse):
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            Path(self.path).unlink(missing_ok=True)
