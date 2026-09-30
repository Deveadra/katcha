from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from katcha.integrations.chatgpt import (
    ChatGPTConnectionError,
    begin_chatgpt_oauth,
    complete_chatgpt_oauth,
    connection_status,
    disconnect,
    list_models,
    set_selected_model,
    test_connection,
)

router = APIRouter(tags=["chatgpt"])


class OAuthStartResponse(BaseModel):
    authorization_url: str


class ChatGPTModelResponse(BaseModel):
    slug: str
    display_name: str


class ChatGPTModelUpdate(BaseModel):
    model: str = Field(min_length=1, max_length=255)


@router.get("/v1/integrations/chatgpt/status")
def chatgpt_status() -> dict[str, object]:
    return connection_status()


@router.post(
    "/v1/integrations/chatgpt/oauth/start",
    response_model=OAuthStartResponse,
)
def chatgpt_oauth_start(
    connection_id: uuid.UUID | None = Query(default=None),
) -> OAuthStartResponse:
    try:
        return OAuthStartResponse(
            authorization_url=begin_chatgpt_oauth(connection_id=connection_id)
        )
    except ChatGPTConnectionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/auth/callback", include_in_schema=False)
def chatgpt_oauth_callback(
    state: str,
    code: str | None = None,
    error: str | None = None,
    client_id: str | None = None,
) -> RedirectResponse:
    if error:
        return RedirectResponse("/settings?chatgpt=error")
    if not code:
        return RedirectResponse("/settings?chatgpt=error")
    try:
        complete_chatgpt_oauth(
            state=state,
            code=code,
            returned_client_id=client_id,
        )
    except ChatGPTConnectionError:
        return RedirectResponse("/settings?chatgpt=error")
    return RedirectResponse("/settings?chatgpt=connected")


@router.get(
    "/v1/integrations/chatgpt/models",
    response_model=list[ChatGPTModelResponse],
)
def chatgpt_models() -> list[ChatGPTModelResponse]:
    try:
        return [
            ChatGPTModelResponse(slug=model.slug, display_name=model.display_name)
            for model in list_models()
        ]
    except ChatGPTConnectionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/v1/integrations/chatgpt/model")
def update_chatgpt_model(request: ChatGPTModelUpdate) -> dict[str, object]:
    try:
        connection = set_selected_model(request.model)
    except ChatGPTConnectionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "ok": True,
        "selected_model": connection.selected_model,
    }


@router.post("/v1/integrations/chatgpt/test")
def test_chatgpt() -> dict[str, object]:
    try:
        return test_connection()
    except ChatGPTConnectionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/v1/integrations/chatgpt/disconnect")
def disconnect_chatgpt() -> dict[str, object]:
    try:
        disconnected = disconnect()
    except ChatGPTConnectionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"ok": True, "disconnected": disconnected}
