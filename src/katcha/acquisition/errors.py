from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx


@dataclass(frozen=True, slots=True)
class ProviderFailure:
    provider: str
    operation: str
    kind: str
    status_code: int | None = None
    retry_after_seconds: int | None = None
    retryable: bool = True


class DiscoveryProviderError(RuntimeError):
    def __init__(self, failure: ProviderFailure) -> None:
        self.failure = failure
        status = f" status={failure.status_code}" if failure.status_code else ""
        retry_after = (
            f" retry_after={failure.retry_after_seconds}s"
            if failure.retry_after_seconds is not None
            else ""
        )
        super().__init__(
            f"{failure.provider} {failure.operation} failed: "
            f"{failure.kind}{status}{retry_after}"
        )


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> int | None:
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        return max(int(float(raw)), 0)
    except ValueError:
        pass
    try:
        target = parsedate_to_datetime(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=UTC)
    reference = now or datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    return max(int((target.astimezone(UTC) - reference.astimezone(UTC)).total_seconds()), 0)


def failure_from_response(
    provider: str,
    operation: str,
    response: httpx.Response,
) -> DiscoveryProviderError:
    status = response.status_code
    retry_after = parse_retry_after(response.headers.get("retry-after"))
    if status == 429:
        kind = "rate_limited"
        retryable = True
    elif 500 <= status <= 599:
        kind = "provider_5xx"
        retryable = True
    elif status in {401, 403}:
        kind = "authentication"
        retryable = False
    elif 400 <= status <= 499:
        kind = "provider_4xx"
        retryable = False
    else:
        kind = "http_error"
        retryable = True
    return DiscoveryProviderError(
        ProviderFailure(
            provider=provider,
            operation=operation,
            kind=kind,
            status_code=status,
            retry_after_seconds=retry_after,
            retryable=retryable,
        )
    )


def failure_from_transport(
    provider: str,
    operation: str,
    exc: httpx.HTTPError,
) -> DiscoveryProviderError:
    del exc
    return DiscoveryProviderError(
        ProviderFailure(
            provider=provider,
            operation=operation,
            kind="transport",
            retryable=True,
        )
    )
