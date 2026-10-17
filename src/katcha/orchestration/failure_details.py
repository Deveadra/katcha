"""Bounded, serializable workflow diagnostics without transport wrapper noise."""

from __future__ import annotations

import re

YOUTUBE_DOWNLOAD_AUTH_RECOVERY = (
    "YouTube challenged the download request from the Katcha server. A public "
    "watch link does not guarantee access to the underlying media streams. "
    "An optional authorized server session may help but is not guaranteed to work; "
    "use an approved direct media source or upload if the challenge persists. "
    "Signing in on your browser does not authenticate Katcha's worker."
)

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
    classification = {"code": None, "retryable": None, "recovery": None}
    while current is not None and id(current) not in seen and len(chain) < 16:
        seen.add(id(current))
        stage = getattr(current, "diagnostic_stage", None) or stage
        for key, attribute in (
            ("code", "diagnostic_code"),
            ("retryable", "diagnostic_retryable"),
            ("recovery", "diagnostic_recovery"),
        ):
            value = getattr(current, attribute, None)
            if value is not None:
                classification[key] = value
        if getattr(current, "type", None) == "MediaIngestionFailure":
            for detail in getattr(current, "details", ()):
                if isinstance(detail, dict) and isinstance(detail.get("stage"), str):
                    stage = detail["stage"]
                    for key in classification:
                        if detail.get(key) is not None:
                            classification[key] = detail[key]
        message = str(current).strip()
        chain.append({"type": type(current).__name__, "message": safe_failure_text(message)})
        if message and message.casefold() not in _GENERIC:
            best = message
        nested = getattr(current, "cause", None)
        if not isinstance(nested, BaseException):
            nested = current.__cause__
        current = nested if isinstance(nested, BaseException) else None
    if classification["code"] is None and "[youtube]" in best.casefold() and (
        "sign in to confirm" in best.casefold() or "sign in to view" in best.casefold()
    ):
        # Older saved failures predate typed downloader errors. Their explicit
        # extractor challenge still supplies evidence for the same recovery.
        classification = {
            "code": "youtube_authentication_required", "retryable": False,
            "recovery": YOUTUBE_DOWNLOAD_AUTH_RECOVERY,
        }
    if stage and best.startswith(f"{stage}: "):
        best = best[len(stage) + 2 :]
    return {
        "message": safe_failure_text(
            (f"{stage}: " if stage else "") + (best or str(exc).strip() or type(exc).__name__)
        ),
        "stage": stage,
        **classification,
        "causes": chain,
    }
