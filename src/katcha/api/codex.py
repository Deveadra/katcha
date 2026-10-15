from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from katcha.integrations.codex import (
    CodexConnectionError,
    begin_codex_oauth,
    complete_codex_callback_url,
    connection_status,
    disconnect,
    list_models,
    set_selected_model,
    test_connection,
    usage,
)

router = APIRouter(tags=["codex"])


class OAuthStartResponse(BaseModel):
    authorization_url: str
    state: str


class OAuthCompleteRequest(BaseModel):
    callback_url: str = Field(min_length=1, max_length=8192)
    expected_state: str = Field(min_length=20, max_length=255)


class CodexModelResponse(BaseModel):
    slug: str
    display_name: str
    description: str | None = None


class CodexModelUpdate(BaseModel):
    model: str = Field(min_length=1, max_length=255)


@router.get("/v1/integrations/codex/status")
def codex_status() -> dict[str, object]:
    return connection_status()


@router.post(
    "/v1/integrations/codex/oauth/start",
    response_model=OAuthStartResponse,
)
def codex_oauth_start() -> OAuthStartResponse:
    try:
        authorization_url = begin_codex_oauth()
        state = parse_qs(urlsplit(authorization_url).query)["state"][0]
        return OAuthStartResponse(authorization_url=authorization_url, state=state)
    except CodexConnectionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/v1/integrations/codex/oauth/complete")
def codex_oauth_complete(request: OAuthCompleteRequest) -> dict[str, object]:
    try:
        complete_codex_callback_url(
            callback_url=request.callback_url, expected_state=request.expected_state
        )
    except CodexConnectionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "connected": True}


@router.get(
    "/v1/integrations/codex/models",
    response_model=list[CodexModelResponse],
)
def codex_models() -> list[CodexModelResponse]:
    try:
        return [
            CodexModelResponse(
                slug=model.slug,
                display_name=model.display_name,
                description=model.description,
            )
            for model in list_models()
        ]
    except CodexConnectionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/v1/integrations/codex/model")
def update_codex_model(request: CodexModelUpdate) -> dict[str, object]:
    try:
        connection = set_selected_model(request.model)
    except CodexConnectionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "selected_model": connection.selected_model}


@router.get("/v1/integrations/codex/usage")
def codex_usage() -> dict[str, object]:
    try:
        return usage()
    except CodexConnectionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/v1/integrations/codex/test")
def test_codex() -> dict[str, object]:
    try:
        return test_connection()
    except CodexConnectionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/v1/integrations/codex/disconnect")
def disconnect_codex() -> dict[str, object]:
    try:
        disconnected = disconnect()
    except CodexConnectionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"ok": True, "disconnected": disconnected}
