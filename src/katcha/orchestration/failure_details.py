"""Bounded, serializable workflow diagnostics without transport wrapper noise."""

from __future__ import annotations

import re

_GENERIC = {
    "activity task failed",
    "child workflow execution failed",
    "workflow execution failed",
}


def safe_failure_text(message: str) -> str:
    # URLs and header values can contain signed credentials. Keep host-independent
    # failure text; identities are carried in separate fields in the receipt.
    message = re.sub(r"https?://[^\s<>\"']+", "[source URL]", message)
    message = re.sub(
        r"(?i)\b(authorization|cookie|set-cookie)\s*[:=][^\r\n]+", r"\1=[redacted]", message
    )
    # Match query-style, header-style, and quoted JSON credential fields.
    keys = r"(?:access[_-]?token|refresh[_-]?token|token|api[_-]?key|password|secret|signature)"
    message = re.sub(
        rf"(?i)([\"']?\b{keys}[\"']?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;}}]+)",
        r"\1[redacted]",
        message,
    )
    return message[:8000]


class MediaPreparationError(RuntimeError):
    def __init__(self, stage: str, cause: Exception):
        self.diagnostic_stage = stage
        super().__init__(f"{stage}: {safe_failure_text(str(cause))}")


def failure_details(exc: BaseException) -> dict:
    seen: set[int] = set()
    current: BaseException | None = exc
    best = ""
    chain = []
    stage = None
    while current is not None and id(current) not in seen and len(chain) < 16:
        seen.add(id(current))
        stage = getattr(current, "diagnostic_stage", None) or stage
        if getattr(current, "type", None) == "MediaIngestionFailure":
            for detail in getattr(current, "details", ()):
                if isinstance(detail, dict) and isinstance(detail.get("stage"), str):
                    stage = detail["stage"]
        message = str(current).strip()
        chain.append({"type": type(current).__name__, "message": safe_failure_text(message)})
        if message and message.casefold() not in _GENERIC:
            best = message
        nested = getattr(current, "cause", None)
        if not isinstance(nested, BaseException):
            nested = current.__cause__
        current = nested if isinstance(nested, BaseException) else None
    if stage and best.startswith(f"{stage}: "):
        best = best[len(stage) + 2 :]
    return {
        "message": safe_failure_text(
            (f"{stage}: " if stage else "") + (best or str(exc).strip() or type(exc).__name__)
        ),
        "stage": stage,
        "causes": chain,
    }
