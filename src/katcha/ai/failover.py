from __future__ import annotations


_SAFE_STATUS_CODES = {401, 403, 404, 429}
_SAFE_CLASS_NAMES = {
    "providerunavailable",
    "scriptproviderunavailable",
    "episodescriptproviderunavailable",
    "ttsunavailable",
    "ratelimiterror",
    "resourceexhausted",
    "toomanyrequests",
}
_SAFE_MESSAGE_MARKERS = (
    "insufficient_quota",
    "credit_balance_exhausted",
    "no credits remaining",
    "quota exceeded",
    "resource exhausted",
    "rate limit",
)


def safe_to_fail_over(exc: BaseException) -> bool:
    """Return True only for explicit provider rejections known not to be accepted work."""
    status = getattr(exc, "status_code", None)
    if status is None:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
    if status is None:
        status = getattr(exc, "code", None)
    try:
        if status is not None and int(status) in _SAFE_STATUS_CODES:
            return True
    except (TypeError, ValueError):
        pass

    if type(exc).__name__.casefold() in _SAFE_CLASS_NAMES:
        return True

    message = str(exc).casefold()
    return any(marker in message for marker in _SAFE_MESSAGE_MARKERS)
