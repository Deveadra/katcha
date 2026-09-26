from __future__ import annotations

from collections.abc import Callable

from katcha.ai.router import ModelTarget

_GEMINI_CAPACITY_FALLBACKS: dict[str, tuple[str, ...]] = {
    "gemini-3.8-flash": (
        "gemini-3.7-flash",
        "gemini-3.6-flash",
    ),
}

_CAPACITY_MARKERS = (
    "high demand",
    "temporarily unavailable",
    "service unavailable",
    "status': 'unavailable'",
    '"status": "unavailable"',
)


def _status_code(exc: BaseException) -> int | None:
    raw = getattr(exc, "status_code", None)
    if raw is None:
        response = getattr(exc, "response", None)
        raw = getattr(response, "status_code", None)
    if raw is None:
        raw = getattr(exc, "code", None)
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def explicit_model_capacity_rejection(exc: BaseException) -> bool:
    """Identify a provider response that clearly rejected work for temporary capacity."""
    if _status_code(exc) != 503:
        return False
    message = str(exc).casefold()
    return any(marker in message for marker in _CAPACITY_MARKERS)


def capacity_targets(target: ModelTarget) -> tuple[ModelTarget, ...]:
    if target.provider != "gemini":
        return (target,)
    alternates = _GEMINI_CAPACITY_FALLBACKS.get(target.model, ())
    return (target, *(ModelTarget("gemini", model) for model in alternates))


def run_with_gemini_capacity_fallback[T](
    target: ModelTarget,
    invoke: Callable[[ModelTarget], T],
) -> T:
    targets = capacity_targets(target)
    for index, candidate in enumerate(targets):
        try:
            return invoke(candidate)
        except Exception as exc:
            is_last = index == len(targets) - 1
            if is_last or not explicit_model_capacity_rejection(exc):
                raise
    raise RuntimeError("Gemini capacity fallback exhausted unexpectedly")
