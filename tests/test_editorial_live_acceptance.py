import importlib.util
import uuid
from pathlib import Path

import pytest


def _load_runner():
    path = Path(__file__).resolve().parents[1] / "scripts" / "editorial_live_acceptance.py"
    spec = importlib.util.spec_from_file_location("editorial_live_acceptance", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = _load_runner()


@pytest.mark.parametrize(
    "url",
    [
        "https://youtube.com/watch?v=abc",
        "https://www.youtube.com/watch?v=abc",
        "https://m.youtube.com/watch?v=abc",
        "https://youtu.be/abc",
    ],
)
def test_live_acceptance_source_url_is_restricted_to_https_youtube(url: str) -> None:
    assert runner._validate_source_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://youtube.com/watch?v=abc",
        "https://example.com/video",
        "https://user:pass@youtube.com/watch?v=abc",
        "https://youtube.com:8443/watch?v=abc",
    ],
)
def test_live_acceptance_rejects_unsupported_source_urls(url: str) -> None:
    with pytest.raises(ValueError, match="HTTPS YouTube"):
        runner._validate_source_url(url)


def test_source_authorization_is_an_explicit_human_gate() -> None:
    with pytest.raises(RuntimeError, match="confirm-source-authorized"):
        runner._require_source_authorization(False)
    runner._require_source_authorization(True)


def test_render_approval_requires_exact_inspected_render() -> None:
    assert (
        runner._review_approval_requested(
            "render-one",
            None,
            preview_inspected=False,
        )
        is False
    )
    with pytest.raises(RuntimeError, match="refusing stale approval"):
        runner._review_approval_requested(
            "render-one",
            "render-two",
            preview_inspected=True,
        )
    with pytest.raises(RuntimeError, match="confirm-preview-inspected"):
        runner._review_approval_requested(
            "render-one",
            "render-one",
            preview_inspected=False,
        )
    assert runner._review_approval_requested(
        "render-one",
        "render-one",
        preview_inspected=True,
    )


def test_provider_receipt_summary_excludes_provider_output_payload() -> None:
    summary = runner._receipt_summary(
        {
            "editorial_run_id": "run",
            "target": "script",
            "artifacts": {
                "provider_calls": {
                    "research": {
                        "status": "completed",
                        "provider": "gemini",
                        "model": "example",
                        "submissions": 1,
                        "input_tokens": 123,
                        "output_tokens": 45,
                        "coverage": "text",
                        "billing_basis": "operator_configured_free_tier",
                        "grounded_urls": ["https://example.com/a", "https://example.com/b"],
                        "value": {"secretly_large_provider_output": "not surfaced"},
                    }
                }
            },
        }
    )

    assert summary == [
        {
            "run_id": "run",
            "target": "script",
            "call_key": "research",
            "status": "completed",
            "provider": "gemini",
            "model": "example",
            "submissions": 1,
            "input_tokens": 123,
            "output_tokens": 45,
            "coverage": "text",
            "billing_basis": "operator_configured_free_tier",
            "grounded_url_count": 2,
        }
    ]


def test_latest_rights_assessment_never_uses_stale_clearance() -> None:
    latest = runner._latest_assessment(
        {
            "assessments": [
                {"id": "older", "version": 1, "production_eligible": True},
                {"id": "newer", "version": 2, "production_eligible": False},
            ]
        }
    )
    assert latest["id"] == "newer"
    assert latest["production_eligible"] is False


def test_start_run_recovers_only_exact_deterministic_request_identity() -> None:
    project_id = uuid.uuid4()
    key_parts = (str(project_id), 1, "asset-a")
    idempotency_key = runner._key("direction", *key_parts)
    expected_run_id = str(
        uuid.uuid5(project_id, f"editorial-run:{idempotency_key}")
    )

    class Client:
        def __init__(self):
            self.posts = []

        def get(self, path: str):
            assert path.endswith(f"/runs/{expected_run_id}")
            return {
                "editorial_run_id": expected_run_id,
                "channel_profile_id": "channel",
                "project_id": str(project_id),
                "input_revision": 1,
                "target": "direction",
                "status": "completed",
                "stage": "visual_plan_ready",
                "artifacts": {},
            }

        def post(self, path: str, payload):
            self.posts.append((path, payload))
            raise AssertionError("exact completed run should be reused without a new POST")

    client = Client()
    result = runner._start_run(
        client,
        f"/v1/channels/channel/editorial-projects/{project_id}",
        revision=1,
        target="direction",
        key_parts=key_parts,
        timeout_seconds=600,
        max_queries=12,
        max_model_calls=30,
        max_model_tokens=1_000_000,
    )

    assert result["editorial_run_id"] == expected_run_id
    assert client.posts == []


def test_live_runtime_rejects_fixture_execution() -> None:
    class Client:
        def get(self, path: str):
            if path == "/v1/health/ready":
                return {"status": "ok"}
            if path == "/v1/runtime/ai":
                return {
                    "execution_mode": "fixture",
                    "external_provider_calls_enabled": False,
                }
            raise AssertionError(path)

    with pytest.raises(RuntimeError, match="execution_mode=live"):
        runner._ensure_runtime(Client())
