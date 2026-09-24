from __future__ import annotations

import uuid

import pytest

from katcha.integrations.youtube import reporting


class FakeResponse:
    def __init__(
        self,
        *,
        payload: dict[str, object] | None = None,
        content: bytes = b"",
        status_code: int = 200,
    ) -> None:
        self._payload = payload or {}
        self.content = content
        self.status_code = status_code
        self.is_error = status_code >= 400
        self.is_redirect = 300 <= status_code < 400
        self.text = content.decode("utf-8", errors="replace")

    def json(self) -> dict[str, object]:
        return self._payload


def test_list_reporting_jobs_paginates(monkeypatch: pytest.MonkeyPatch) -> None:
    connection_id = uuid.uuid4()
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        reporting,
        "get_valid_access_token",
        lambda *_args, **_kwargs: "token",
    )

    def fake_request(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        token = (kwargs.get("params") or {}).get("pageToken")
        if token is None:
            return FakeResponse(
                payload={
                    "jobs": [{"id": "one", "reportTypeId": "other"}],
                    "nextPageToken": "next",
                }
            )
        return FakeResponse(
            payload={
                "jobs": [
                    {
                        "id": "reach",
                        "reportTypeId": reporting.REACH_REPORT_TYPE,
                    }
                ]
            }
        )

    monkeypatch.setattr(reporting.httpx, "request", fake_request)
    jobs = reporting.list_reporting_jobs(connection_id)

    assert [job["id"] for job in jobs] == ["one", "reach"]
    assert calls[0]["params"] == {
        "pageSize": 100,
        "includeSystemManaged": True,
    }
    assert calls[1]["params"] == {
        "pageSize": 100,
        "includeSystemManaged": True,
        "pageToken": "next",
    }


def test_create_reporting_job_uses_official_reach_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection_id = uuid.uuid4()
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        reporting,
        "get_valid_access_token",
        lambda *_args, **_kwargs: "token",
    )

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return FakeResponse(
            payload={
                "id": "provider-job",
                "reportTypeId": reporting.REACH_REPORT_TYPE,
            }
        )

    monkeypatch.setattr(reporting.httpx, "request", fake_request)
    result = reporting.create_reporting_job(connection_id)

    assert result["id"] == "provider-job"
    assert captured["method"] == "POST"
    assert captured["url"] == f"{reporting.REPORTING_ROOT}/jobs"
    assert captured["json"] == {
        "reportTypeId": "channel_reach_basic_a1",
        "name": "katcha-channel-reach",
    }


def test_report_download_rejects_untrusted_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        reporting,
        "get_valid_access_token",
        lambda *_args, **_kwargs: "token",
    )
    with pytest.raises(reporting.YouTubeReportingError, match="unexpected host"):
        reporting.download_report(
            uuid.uuid4(),
            "https://example.com/report.csv",
        )


def test_report_download_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        reporting,
        "get_valid_access_token",
        lambda *_args, **_kwargs: "token",
    )
    monkeypatch.setattr(
        reporting.httpx,
        "get",
        lambda *_args, **_kwargs: FakeResponse(content=b"x" * 11),
    )

    with pytest.raises(reporting.YouTubeReportingError, match="byte limit"):
        reporting.download_report(
            uuid.uuid4(),
            "https://youtubereporting.googleapis.com/v1/media/report",
            max_bytes=10,
        )
