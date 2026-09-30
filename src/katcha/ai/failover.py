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
_SAFE_CAPACITY_MARKERS = (
    "high demand",
    "temporarily unavailable",
    "status': 'unavailable'",
    '"status": "unavailable"',
)

_SAFE_MESSAGE_MARKERS = (
    "insufficient_quota",
    "credit_balance_exhausted",
    "no credits remaining",
    "quota exceeded",
    "resource exhausted",
    "rate limit",
)

_GENERATION_FAILOVER_STATUS_CODES = {400, 408, 422, 500, 502, 503, 504}
_GENERATION_FAILOVER_CLASS_NAMES = {
    "apiconnectionerror",
    "apitimeouterror",
    "badrequesterror",
    "connecterror",
    "connectionerror",
    "connecttimeout",
    "internalservererror",
    "readerror",
    "readtimeout",
    "timeout",
    "timeouterror",
}


def _status_code(exc: BaseException) -> int | None:
    status = getattr(exc, "status_code", None)
    if status is None:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
    if status is None:
        status = getattr(exc, "code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def safe_to_fail_over(exc: BaseException) -> bool:
    """Return True only for explicit provider rejections known not to be accepted work."""
    normalized_status = _status_code(exc)
    if normalized_status in _SAFE_STATUS_CODES:
        return True

    message = str(exc).casefold()
    if normalized_status == 503 and any(
        marker in message for marker in _SAFE_CAPACITY_MARKERS
    ):
        return True

    if type(exc).__name__.casefold() in _SAFE_CLASS_NAMES:
        return True

    return any(marker in message for marker in _SAFE_MESSAGE_MARKERS)


def safe_to_fail_over_generation(exc: BaseException) -> bool:
    """Allow another provider for read-only inference when the first cannot answer.

    Command planning and narration do not mutate Katcha state. They can therefore
    use a broader failover policy than workflow-producing provider calls without
    risking duplicate actions. Budget accounting and explicit action confirmation
    remain unchanged.
    """
    if safe_to_fail_over(exc):
        return True
    if _status_code(exc) in _GENERATION_FAILOVER_STATUS_CODES:
        return True
    return type(exc).__name__.casefold() in _GENERATION_FAILOVER_CLASS_NAMES
