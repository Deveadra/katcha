"""Browser RPC shared by goal workers and the authenticated operator handoff."""
from __future__ import annotations

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
    }


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
    except (httpx.HTTPError, ValueError):
        return {"available": False, "code": "browser_connection_lost", "outcome_uncertain": True,
                "message": "The server browser connection was interrupted. Refresh the page view "
                           "to check the result before repeating an action."}
