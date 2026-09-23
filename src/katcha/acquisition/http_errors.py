"""Secret-safe provider HTTP failure normalization for discovery adapters."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from katcha.acquisition.adapters import DiscoveryProviderError


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> int | None:
    """Accept seconds or an HTTP-date; do not underestimate a server's delay."""
    if not value or not value.strip():
        return None
    raw = value.strip()
    if raw.isdecimal():
        return max(int(raw), 0)
    try:
        target = parsedate_to_datetime(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=UTC)
    reference = now or datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    return max(math.ceil((target.astimezone(UTC) - reference.astimezone(UTC)).total_seconds()), 0)


def provider_response_error(
    provider: str, operation: str, response: httpx.Response
) -> DiscoveryProviderError:
    """Never include response body, request URL, headers or credentials."""
    status = response.status_code
    if status == 429:
        kind, transient = "rate_limited", True
    elif status >= 500:
        kind, transient = "provider_unavailable", True
    elif status in {401, 403}:
        kind, transient = "provider_configuration", False
    else:
        kind, transient = "provider_error", False
    return DiscoveryProviderError(
        f"{provider} {operation} request failed",
        kind=kind,
        transient=transient,
        status_code=status,
        retry_after_seconds=parse_retry_after(response.headers.get("Retry-After")),
    )


def provider_transport_error(provider: str, operation: str) -> DiscoveryProviderError:
    """Keep exception chains for debugging but exclude secret-bearing URLs from the message."""
    return DiscoveryProviderError(
        f"{provider} {operation} request failed",
        kind="transport_error",
        transient=True,
    )


def provider_payload_error(provider: str, operation: str) -> DiscoveryProviderError:
    return DiscoveryProviderError(
        f"{provider} {operation} returned invalid data",
        kind="invalid_provider_response",
        transient=False,
    )
