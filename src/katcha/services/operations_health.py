"""Read the host's sanitized snapshot; the API never gains host control privileges."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

SNAPSHOT = Path("/operations-health/health.json")
MAX_AGE_SECONDS = 900


def read_health(*, now: float | None = None) -> dict[str, Any]:
    current = time.time() if now is None else now
    try:
        if SNAPSHOT.is_symlink() or SNAPSHOT.stat().st_size > 2_000_000:
            raise ValueError("Invalid snapshot")
        data = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        checked = data["checked_at"]
        if (
            data.get("version") != 1
            or type(checked) not in {float, int}
            or not 0 < checked <= current + 60
            or not all(isinstance(data[key], list) for key in ("signals", "incidents", "history"))
            or not all(
                isinstance(row, dict)
                for key in ("signals", "incidents", "history")
                for row in data[key]
            )
            or not isinstance(data.get("notifications"), dict)
            or type(data["notifications"].get("pending")) is not int
            or data["notifications"]["pending"] < 0
        ):
            raise ValueError("Invalid snapshot")
    except (OSError, ValueError, KeyError, TypeError):
        return {
            "status": "unavailable",
            "checked_at": None,
            "signals": [],
            "incidents": [],
            "history": [],
            "notifications": {"status": "unverified", "pending": 0},
            "message": "Operations monitoring has no current report. Check the host monitor "
            "installation and its read-only dashboard connection.",
            "deployment_authorized": False,
        }
    data["deployment_authorized"] = False
    if current - checked > MAX_AGE_SECONDS:
        data["status"] = "stale"
        data["message"] = "Operations monitoring stopped reporting. Inspect the host monitor; "
        data["message"] += "the last report below is historical."
    else:
        attention = any(row.get("resolved") is None for row in data["incidents"])
        delivery = data["notifications"].get("status") == "failed"
        data["status"] = "attention" if attention or delivery else "healthy"
        data["message"] = (
            "Operations need attention."
            if attention or delivery
            else "Operations checks are current."
        )
    return data
