"""Browser RPC shared by goal workers and the authenticated operator handoff."""
from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

import httpx

from katcha.config import get_settings


def browser_configuration() -> dict:
    settings = get_settings()
    configured = bool(settings.browser_service_url and settings.browser_service_token)
    return {
        "configured": configured,
        "tool": "browser_action",
        "operator_path": "/v1/ai/goals/{goal_id}/browser",
        "coverage": "Server-side Chromium: JavaScript, forms, tabs, screenshots and cookies",
        "limitations": [
            "Sessions expire after 15 idle minutes or a browser service restart",
            "Sign-in must happen in this server session; local browser cookies are not copied",
            "Public HTTPS only; media access does not establish usage rights or scene verification",
        ],
        "live_browser_verified": False,
        "service_status": "not_checked",
        "verification_scope": (
            "A live Chromium health check does not verify external site access or video content"
        ),
    }


async def probe_browser_service() -> dict:
    """Bounded, non-mutating live Chromium health check; never exposes upstream errors."""
    settings = get_settings()
    result = browser_configuration()
    result["checked_at"] = datetime.now(UTC).isoformat()
    if not result["configured"]:
        return {**result, "service_status": "not_configured",
                "code": "browser_not_configured"}
    try:
        # /health starts Chromium if necessary but never opens a provider page.
        # The URL is operator configuration, not a model- or browser-supplied URL.
        async with httpx.AsyncClient(trust_env=False, timeout=5.0) as client:
            response = await client.get(settings.browser_service_url.rstrip("/") + "/health")
        if response.status_code != 200:
            return {**result, "service_status": "unhealthy",
                    "code": "browser_health_unhealthy"}
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("ready") is not True:
            return {**result, "service_status": "unhealthy",
                    "code": "browser_health_invalid"}
    except httpx.TimeoutException:
        return {**result, "service_status": "unreachable",
                "code": "browser_health_timeout"}
    except (httpx.HTTPError, ValueError, TypeError):
        return {**result, "service_status": "unreachable",
                "code": "browser_health_unreachable"}
    return {**result, "service_status": "ready", "live_browser_verified": True,
            "code": "browser_service_ready"}


async def run_browser(goal, arguments: dict) -> dict:
    settings = get_settings()
    if not settings.browser_service_url or not settings.browser_service_token:
        return {"available": False, "code": "browser_not_configured",
                "message": "The server browser is unavailable. An operator must start the browser "
                           "service and configure its connection before opening a page."}
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=50) as client:
            response = await client.post(
                settings.browser_service_url.rstrip("/") + "/execute",
                headers={"Authorization": "Bearer " +
                         settings.browser_service_token.get_secret_value()},
                json={"scope": [goal.actor, str(goal.channel_profile_id), str(goal.id)],
                      "arguments": arguments},
            )
            response.raise_for_status()
            return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        return browser_transport_failure(exc)


def browser_transport_failure(exc: Exception) -> dict:
    """Sanitized RPC diagnostic shared by AI tools and the operator browser panel."""
    diagnostic_id = uuid.uuid4().hex
    uncertain = True
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in {401, 403}:
            code = "browser_service_auth_rejected"
            message = "The browser service rejected Katcha's internal authorization."
            uncertain = False
        elif status == 503:
            code = "browser_service_unavailable"
            message = "The browser service is not ready. Check its health before retrying."
        else:
            code = "browser_service_http_failure"
            message = "The browser service returned an unsuccessful response."
    elif isinstance(exc, httpx.TimeoutException):
        code = "browser_rpc_timeout"
        message = "The browser RPC timed out. Inspect the session before repeating the action."
    elif isinstance(exc, ValueError):
        code = "browser_invalid_response"
        message = "The browser service returned an invalid response. Inspect service health."
    else:
        code = "browser_connection_lost"
        message = ("The server browser connection was interrupted. Refresh the page view "
                   "to check the result before repeating an action.")
    # Never log transport exception strings: they can contain URLs, tokens or form data.
    logging.getLogger(__name__).warning(
        "browser_transport_failure diagnostic_id=%s code=%s exception=%s",
        diagnostic_id, code, type(exc).__name__,
    )
    return {"available": False, "code": code, "stage": "service_rpc",
            "diagnostic_id": diagnostic_id, "exception_class": type(exc).__name__,
            "outcome_uncertain": uncertain, "message": message}
