from __future__ import annotations

import json
from typing import Any

import httpx

from katcha.config import Settings, get_settings


class TelegramAPIError(RuntimeError):
    pass


class TelegramBotClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        secret = self.settings.telegram_bot_token
        token = secret.get_secret_value().strip() if secret else ""
        if not token:
            raise TelegramAPIError("Telegram bot token is not configured")
        self.base_url = f"https://api.telegram.org/bot{token}"

    def _result(self, response: httpx.Response, method: str) -> Any:
        try:
            payload = response.json()
        except ValueError as exc:
            raise TelegramAPIError(
                f"Telegram {method} returned invalid JSON"
            ) from exc
        if response.is_error or not payload.get("ok"):
            description = str(payload.get("description") or response.text)[:1000]
            raise TelegramAPIError(f"Telegram {method} failed: {description}")
        return payload.get("result")

    def get_me(self) -> dict[str, Any]:
        response = httpx.get(f"{self.base_url}/getMe", timeout=20)
        result = self._result(response, "getMe")
        return dict(result or {})

    def delete_webhook(self) -> None:
        response = httpx.post(
            f"{self.base_url}/deleteWebhook",
            json={"drop_pending_updates": False},
            timeout=20,
        )
        self._result(response, "deleteWebhook")

    def get_updates(
        self,
        *,
        offset: int | None,
        timeout_seconds: int,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "timeout": timeout_seconds,
            "limit": 50,
            "allowed_updates": json.dumps(["message", "callback_query"]),
        }
        if offset is not None:
            params["offset"] = offset
        response = httpx.get(
            f"{self.base_url}/getUpdates",
            params=params,
            timeout=timeout_seconds + 10,
        )
        result = self._result(response, "getUpdates")
        return [dict(item) for item in list(result or [])]

    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        response = httpx.post(
            f"{self.base_url}/sendMessage",
            json=payload,
            timeout=30,
        )
        return dict(self._result(response, "sendMessage") or {})

    def send_video(
        self,
        chat_id: int,
        *,
        video: bytes | str,
        filename: str,
        caption: str,
        reply_markup: dict[str, Any],
    ) -> dict[str, Any]:
        data = {
            "chat_id": str(chat_id),
            "caption": caption,
            "supports_streaming": "true",
            "reply_markup": json.dumps(reply_markup, separators=(",", ":")),
        }
        if isinstance(video, str):
            data["video"] = video
            response = httpx.post(
                f"{self.base_url}/sendVideo",
                data=data,
                timeout=60,
            )
        else:
            response = httpx.post(
                f"{self.base_url}/sendVideo",
                data=data,
                files={"video": (filename, video, "video/mp4")},
                timeout=180,
            )
        return dict(self._result(response, "sendVideo") or {})

    def answer_callback(
        self,
        callback_query_id: str,
        *,
        text: str | None = None,
        show_alert: bool = False,
    ) -> None:
        payload: dict[str, Any] = {
            "callback_query_id": callback_query_id,
            "show_alert": show_alert,
        }
        if text:
            payload["text"] = text[:200]
        response = httpx.post(
            f"{self.base_url}/answerCallbackQuery",
            json=payload,
            timeout=20,
        )
        self._result(response, "answerCallbackQuery")

    def edit_caption(
        self,
        chat_id: int,
        message_id: int,
        *,
        caption: str,
        reply_markup: dict[str, Any],
    ) -> dict[str, Any]:
        response = httpx.post(
            f"{self.base_url}/editMessageCaption",
            json={
                "chat_id": chat_id,
                "message_id": message_id,
                "caption": caption,
                "reply_markup": reply_markup,
            },
            timeout=30,
        )
        return dict(self._result(response, "editMessageCaption") or {})

    def clear_buttons(self, chat_id: int, message_id: int) -> None:
        response = httpx.post(
            f"{self.base_url}/editMessageReplyMarkup",
            json={
                "chat_id": chat_id,
                "message_id": message_id,
                "reply_markup": {"inline_keyboard": []},
            },
            timeout=30,
        )
        self._result(response, "editMessageReplyMarkup")
