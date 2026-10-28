"""Regression coverage for Telegram API credentials appearing in transport logs."""

from __future__ import annotations

import logging
import sys

import httpx
import pytest

from katcha.config import Settings
from katcha.integrations.telegram import (
    TelegramAPIError,
    TelegramBotClient,
    install_telegram_log_redaction,
    redact_telegram_secrets,
)
from katcha.telegram_worker import _TelegramSafeFormatter


TOKEN = "123456789:AA" + "testSecretToken01234567890"
URL = f"https://api.telegram.org/bot{TOKEN}/getUpdates"


@pytest.mark.parametrize(
    "raw",
    [
        f'HTTP Request: GET {URL}?timeout=10 "HTTP/1.1 200 OK"',
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        f"https://api.telegram.org/bot{TOKEN.replace(':', '%3A')}/getMe",
        f"RequestError({URL!r})",
        TOKEN,
    ],
)
def test_redacts_telegram_token_in_all_supported_log_forms(raw: str) -> None:
    safe = redact_telegram_secrets(raw)
    assert TOKEN not in safe
    assert TOKEN.replace(":", "%3A") not in safe
    assert "testSecretToken01234567890" not in safe
    assert "bot[REDACTED]" in safe


def test_httpx_records_are_redacted_before_propagating_to_handlers(caplog) -> None:
    install_telegram_log_redaction()
    install_telegram_log_redaction()  # idempotent
    with caplog.at_level(logging.INFO, logger="httpx"):
        logging.getLogger("httpx").info(
            'HTTP Request: GET %s "HTTP/1.1 200 OK"', URL
        )
    assert TOKEN not in caplog.text
    assert "bot[REDACTED]/getUpdates" in caplog.text
    assert "HTTP/1.1 200 OK" in caplog.text


def test_httpcore_debug_request_path_is_redacted(caplog) -> None:
    install_telegram_log_redaction()
    with caplog.at_level(logging.DEBUG, logger="httpcore.http11"):
        logging.getLogger("httpcore.http11").debug("send_request_headers path=%s", URL)
    assert TOKEN not in caplog.text
    assert "bot[REDACTED]/getUpdates" in caplog.text


def test_worker_formatter_redacts_exception_traceback() -> None:
    formatter = _TelegramSafeFormatter("%(levelname)s %(message)s")
    try:
        raise RuntimeError(f"request to {URL} failed")
    except RuntimeError:
        info = sys.exc_info()
    record = logging.LogRecord(
        "katcha.telegram", logging.ERROR, __file__, 1,
        f"Telegram request failed for {URL}", (), info,
    )
    output = formatter.format(record)
    assert TOKEN not in output
    assert output.count("bot[REDACTED]") >= 2
    assert "RuntimeError" in output


def test_telegram_api_error_description_is_safe() -> None:
    client = TelegramBotClient(
        Settings(_env_file=None, telegram_bot_token=TOKEN)
    )
    response = httpx.Response(
        401,
        json={"ok": False, "description": f"Invalid bot token {TOKEN}"},
        request=httpx.Request("GET", URL),
    )
    with pytest.raises(TelegramAPIError) as exc:
        client._result(response, "getMe")
    assert TOKEN not in str(exc.value)
    assert "bot[REDACTED]" in str(exc.value)


@pytest.mark.parametrize("method", ["get_me", "delete_webhook"])
def test_transport_error_never_exposes_telegram_url(monkeypatch, method: str) -> None:
    client = TelegramBotClient(Settings(_env_file=None, telegram_bot_token=TOKEN))

    def broken_request(*args, **kwargs):
        raise httpx.ConnectError(f"connection failed at {URL}")

    monkeypatch.setattr(httpx, "get" if method == "get_me" else "post", broken_request)
    with pytest.raises(TelegramAPIError) as exc:
        getattr(client, method)()
    assert TOKEN not in str(exc.value)
    assert "ConnectError" in str(exc.value)
