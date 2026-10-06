"""Same-origin range playback for short-lived Editorial source grants."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from katcha.api.studio import _stream_object
from katcha.editorial.playback import playback_cookie_name, resolve_playback_grant

router = APIRouter(prefix="/v1/editorial-playback", tags=["editorial-playback"])


@router.get("/{ticket_id}")
def stream_editorial_source(
    ticket_id: uuid.UUID,
    request: Request,
) -> StreamingResponse:
    token = request.cookies.get(playback_cookie_name(ticket_id))
    try:
        source = resolve_playback_grant(ticket_id, token)
    except ValueError as exc:
        message = str(exc)
        if "expired" in message:
            raise HTTPException(status_code=410, detail=message) from exc
        if "not available" in message or "invalid" in message:
            raise HTTPException(status_code=401, detail="Playback authorization required") from exc
        raise HTTPException(status_code=409, detail=message) from exc
    return _stream_object(request, source.storage_key, source.filename)
