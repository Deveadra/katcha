from __future__ import annotations

import httpx
import pytest

from katcha.ops.external_compute_budget import (
    ExternalComputeBudgetClient,
    ExternalComputeBudgetError,
)


def test_reserve_sends_budget_identity_and_parses_reservation(monkeypatch) -> None:
    captured = {}

    def fake_post(url, *, headers, json, timeout):
        captured.update(
            {
                "url": url,
                "headers": headers,
                "json": json,
                "timeout": timeout,
            }
        )
        return httpx.Response(
            201,
            json={
                "reservation": {
                    "id": "reservation-1",
                    "status": "reserved",
                    "provider": "oci",
                    "job_key": "job-1",
                    "retry_group": "incident-1",
                    "estimated_cost_microusd": 1_200_000,
                },
                "reused": False,
            },
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    client = ExternalComputeBudgetClient(
        coordinator_url="https://recovery.example.test",
        token="compute-token",
    )

    reservation = client.reserve(
        job_key="job-1",
        provider="oci",
        operation="paid-control-plane-fallback",
        retry_group="incident-1",
        attempt=1,
        estimated_cost_microusd=1_200_000,
        ttl_seconds=43_200,
        metadata={"availability_domain": "AD-1"},
    )

    assert reservation.id == "reservation-1"
    assert reservation.status == "reserved"
    assert reservation.estimated_cost_microusd == 1_200_000
    assert captured["headers"]["Authorization"] == "Bearer compute-token"
    assert captured["json"]["attempt"] == 1


def test_client_fails_closed_on_coordinator_denial(monkeypatch) -> None:
    def fake_post(url, *, headers, json, timeout):
        del headers, json, timeout
        return httpx.Response(
            402,
            json={"error": "external compute monthly budget exceeded"},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    client = ExternalComputeBudgetClient(
        coordinator_url="https://recovery.example.test",
        token="compute-token",
    )

    with pytest.raises(
        ExternalComputeBudgetError,
        match="monthly budget exceeded",
    ):
        client.reserve(
            job_key="job-1",
            provider="oci",
            operation="paid-control-plane-fallback",
            retry_group="incident-1",
            attempt=1,
            estimated_cost_microusd=1_200_000,
            ttl_seconds=43_200,
        )


def test_client_requires_https_and_token() -> None:
    with pytest.raises(ExternalComputeBudgetError, match="must use HTTPS"):
        ExternalComputeBudgetClient(
            coordinator_url="http://recovery.example.test",
            token="token",
        )

    with pytest.raises(ExternalComputeBudgetError, match="token is required"):
        ExternalComputeBudgetClient(
            coordinator_url="https://recovery.example.test",
            token="",
        )



def test_reserve_accepts_coordinator_assigned_attempt(monkeypatch) -> None:
    def fake_post(url, *, headers, json, timeout):
        del headers, timeout
        assert json["attempt"] is None
        return httpx.Response(
            201,
            json={
                "reservation": {
                    "id": "reservation-auto",
                    "status": "reserved",
                    "provider": "aws-lambda",
                    "job_key": "render-job",
                    "retry_group": "render-output",
                    "attempt": 2,
                    "estimated_cost_microusd": 500_000,
                },
                "reused": False,
            },
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    client = ExternalComputeBudgetClient(
        coordinator_url="https://recovery.example.test",
        token="compute-token",
    )

    reservation = client.reserve(
        job_key="render-job",
        provider="aws-lambda",
        operation="remotion-video-render",
        retry_group="render-output",
        attempt=None,
        estimated_cost_microusd=500_000,
        ttl_seconds=7200,
    )

    assert reservation.attempt == 2
    assert reservation.provider == "aws-lambda"
