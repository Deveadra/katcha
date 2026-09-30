from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from katcha.config import get_settings
from katcha.integrations.chatgpt import (
    ChatGPTConnectionError,
    begin_chatgpt_oauth,
    complete_chatgpt_oauth,
    connection_status,
    consume_failed_oauth,
    disconnect,
    list_models,
    set_selected_model,
    test_connection,
)
from katcha.integrations.codex import (
    CodexConnectionError,
    complete_codex_oauth,
    consume_failed_codex_oauth,
    is_codex_oauth_state,
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
    if is_codex_oauth_state(state):
        if error:
            try:
                consume_failed_codex_oauth(state)
            except CodexConnectionError:
                return RedirectResponse(
                    "http://127.0.0.1:8765/settings?codex=state_error"
                )
            return RedirectResponse("http://127.0.0.1:8765/settings?codex=error")
        if not code:
            return RedirectResponse("http://127.0.0.1:8765/settings?codex=error")
        try:
            complete_codex_oauth(state=state, code=code)
        except CodexConnectionError:
            return RedirectResponse("http://127.0.0.1:8765/settings?codex=error")
        return RedirectResponse("http://127.0.0.1:8765/settings?codex=connected")

    if error:
        try:
            consume_failed_oauth(state)
        except ChatGPTConnectionError:
            return RedirectResponse("/settings?chatgpt=state_error")
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


@router.get("/v1/integrations/ai/providers")
def ai_provider_status() -> dict[str, object]:
    settings = get_settings()
    return {
        "openai": {
            "configured": bool(settings.openai_api_key),
            "model": "gpt-5.6-luna",
            "mode": "api_key",
        },
        "gemini": {
            "configured": bool(settings.gemini_api_key),
            "model": "gemini-3.5-flash-lite",
            "mode": "api_key",
        },
        "chatgpt": connection_status(),
    }


@router.post("/v1/integrations/ai/providers/{provider}/test")
def test_api_provider(provider: str) -> dict[str, object]:
    settings = get_settings()
    try:
        if provider == "openai":
            if not settings.openai_api_key:
                raise ChatGPTConnectionError("OpenAI API key is not configured")
            from openai import OpenAI

            model = "gpt-5.6-luna"
            OpenAI(
                api_key=settings.openai_api_key,
                timeout=20.0,
                max_retries=0,
            ).models.retrieve(model)
            return {
                "ok": True,
                "provider": provider,
                "model": model,
                "detail": (
                    "Credential and model access confirmed. Inference quota/billing "
                    "is checked by the provider when a generation request runs."
                ),
            }

        if provider == "gemini":
            if not settings.gemini_api_key:
                raise ChatGPTConnectionError("Gemini API key is not configured")
            from google import genai

            model = "gemini-3.5-flash-lite"
            client = genai.Client(api_key=settings.gemini_api_key)
            try:
                client.models.get(model=model)
            finally:
                close = getattr(client, "close", None)
                if callable(close):
                    close()
            return {
                "ok": True,
                "provider": provider,
                "model": model,
                "detail": (
                    "Credential and model access confirmed. Inference quota/billing "
                    "is checked by the provider when a generation request runs."
                ),
            }

        raise HTTPException(status_code=404, detail="Unknown AI provider")
    except HTTPException:
        raise
    except Exception as exc:
        detail = " ".join(str(exc).split())[:500]
        raise HTTPException(
            status_code=503,
            detail=(
                f"{provider} provider check failed: {type(exc).__name__}"
                + (f": {detail}" if detail else "")
            ),
        ) from exc
