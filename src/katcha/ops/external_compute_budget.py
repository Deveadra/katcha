from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


class ExternalComputeBudgetError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ExternalComputeReservation:
    id: str
    status: str
    provider: str
    job_key: str
    retry_group: str
    estimated_cost_microusd: int
    attempt: int
    reused: bool


class ExternalComputeBudgetClient:
    """Strongly-consistent budget gate for paid external compute."""

    def __init__(
        self,
        *,
        coordinator_url: str,
        token: str,
        timeout_seconds: float = 15.0,
    ) -> None:
        self.base = coordinator_url.rstrip("/")
        self.token = token.strip()
        self.timeout_seconds = timeout_seconds
        if not self.base.startswith("https://"):
            raise ExternalComputeBudgetError(
                "external-compute coordinator URL must use HTTPS"
            )
        if not self.token:
            raise ExternalComputeBudgetError(
                "external-compute coordinator token is required"
            )

    def _request(
        self,
        path: str,
        *,
        payload: dict[str, object],
    ) -> dict[str, Any]:
        try:
            response = httpx.post(
                f"{self.base}{path}",
                headers={"Authorization": f"Bearer {self.token}"},
                json=payload,
                timeout=self.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise ExternalComputeBudgetError(
                f"external-compute coordinator request failed: {path}"
            ) from exc
        if response.status_code >= 400:
            raise ExternalComputeBudgetError(
                f"external-compute coordinator {path} returned "
                f"HTTP {response.status_code}: {response.text[:300]}"
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise ExternalComputeBudgetError(
                f"external-compute coordinator {path} returned invalid JSON"
            ) from exc
        if not isinstance(data, dict):
            raise ExternalComputeBudgetError(
                f"external-compute coordinator {path} returned invalid JSON"
            )
        return data

    def reserve(
        self,
        *,
        job_key: str,
        provider: str,
        operation: str,
        retry_group: str,
        attempt: int | None,
        estimated_cost_microusd: int,
        ttl_seconds: int,
        metadata: dict[str, object] | None = None,
    ) -> ExternalComputeReservation:
        data = self._request(
            "/v1/external-compute/reserve",
            payload={
                "job_key": job_key,
                "provider": provider,
                "operation": operation,
                "retry_group": retry_group,
                "attempt": attempt,
                "estimated_cost_microusd": estimated_cost_microusd,
                "ttl_seconds": ttl_seconds,
                "metadata": metadata or {},
            },
        )
        row = data.get("reservation")
        if not isinstance(row, dict):
            raise ExternalComputeBudgetError(
                "external-compute reserve returned no reservation"
            )
        reservation_id = str(row.get("id") or "").strip()
        status = str(row.get("status") or "").strip()
        if not reservation_id or not status:
            raise ExternalComputeBudgetError(
                "external-compute reserve returned an invalid reservation"
            )
        return ExternalComputeReservation(
            id=reservation_id,
            status=status,
            provider=str(row.get("provider") or provider),
            job_key=str(row.get("job_key") or job_key),
            retry_group=str(row.get("retry_group") or retry_group),
            estimated_cost_microusd=int(
                row.get("estimated_cost_microusd")
                or estimated_cost_microusd
            ),
            attempt=int(row.get("attempt") or attempt or 0),
            reused=bool(data.get("reused")),
        )

    def settle(
        self,
        reservation_id: str,
        *,
        actual_cost_microusd: int,
        metadata: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "/v1/external-compute/settle",
            payload={
                "reservation_id": reservation_id,
                "actual_cost_microusd": actual_cost_microusd,
                "metadata": metadata or {},
            },
        )

    def release(
        self,
        reservation_id: str,
        *,
        reason: str,
    ) -> dict[str, Any]:
        return self._request(
            "/v1/external-compute/release",
            payload={
                "reservation_id": reservation_id,
                "reason": reason,
            },
        )
