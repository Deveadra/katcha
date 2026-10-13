"""Channel-authorized, control-plane endpoints for direct R2 multipart uploads."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from katcha.api.control_auth import (
    control_actor,
    require_control_channel,
    require_control_scope,
)
from katcha.editorial import source_multipart

router = APIRouter(
    prefix="/v1/channels/{channel_profile_id}/editorial-projects/source-uploads/multipart",
    tags=["editorial-projects"],
)


class BeginUpload(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    title: str | None = Field(default=None, max_length=300)
    size_bytes: int = Field(gt=0, le=source_multipart.MAX_UPLOAD_BYTES)
    content_type: str = Field(min_length=1, max_length=100)
    idempotency_key: str = Field(min_length=1, max_length=120)
    permitted_use: bool


class SignPart(BaseModel):
    part_number: int = Field(ge=1, le=10000)


def _auth(request: Request, channel_id: uuid.UUID, *, write: bool) -> None:
    require_control_channel(request, channel_id)
    require_control_scope(request, "production:create" if write else "ai:read")


def _bad_request(exc: ValueError) -> HTTPException:
    return HTTPException(409 if "identity" in str(exc) or "no longer" in str(exc) else 422, str(exc))


@router.post("", status_code=201)
async def begin(channel_profile_id: uuid.UUID, body: BeginUpload, request: Request):
    _auth(request, channel_profile_id, write=True)
    try:
        return await run_in_threadpool(
            source_multipart.initiate,
            channel_profile_id,
            filename=body.filename,
            title=body.title,
            size_bytes=body.size_bytes,
            content_type=body.content_type,
            permitted_use=body.permitted_use,
            idempotency_key=body.idempotency_key,
        )
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.get("/{source_id}")
async def inspect(channel_profile_id: uuid.UUID, source_id: uuid.UUID, request: Request):
    _auth(request, channel_profile_id, write=False)
    try:
        return await run_in_threadpool(source_multipart.status, channel_profile_id, source_id)
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.post("/{source_id}/parts")
async def sign(channel_profile_id: uuid.UUID, source_id: uuid.UUID, body: SignPart, request: Request):
    _auth(request, channel_profile_id, write=True)
    try:
        return await run_in_threadpool(
            source_multipart.sign_part, channel_profile_id, source_id, body.part_number
        )
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.post("/{source_id}/complete")
async def complete(channel_profile_id: uuid.UUID, source_id: uuid.UUID, request: Request):
    _auth(request, channel_profile_id, write=True)
    try:
        return await run_in_threadpool(
            source_multipart.finish,
            channel_profile_id,
            source_id,
            actor=control_actor(request),
        )
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.delete("/{source_id}")
async def abort(channel_profile_id: uuid.UUID, source_id: uuid.UUID, request: Request):
    _auth(request, channel_profile_id, write=True)
    try:
        return await run_in_threadpool(source_multipart.cancel, channel_profile_id, source_id)
    except ValueError as exc:
        raise _bad_request(exc) from exc
