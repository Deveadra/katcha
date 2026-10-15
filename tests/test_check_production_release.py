"""Fail-closed contracts for release evidence, without network or OCI calls."""
from __future__ import annotations

import pytest

from katcha.ops import release_eligibility as release

SHA = "a" * 40


def run(workflow: str, event: str, ident: int, **changes: object) -> dict:
    record = {
        "path": workflow,
        "event": event,
        "id": ident,
        "head_sha": SHA,
        "head_branch": "main",
        "repository": {"full_name": release.REPOSITORY},
        "status": "completed",
        "conclusion": "success",
        "updated_at": "2026-10-13T15:01:00Z",
        "run_started_at": "2026-10-13T15:02:00Z",
    }
    record.update(changes)
    return record


def success() -> tuple[list[dict], list[dict]]:
    return [run(release.CI_WORKFLOW, "push", 1)], [
        run(release.IMAGES_WORKFLOW, "workflow_run", 2)
    ]


def test_release_requires_two_ordered_successful_runs() -> None:
    ci, images = success()
    result = release.verify_evidence(SHA, SHA, ci, images)
    assert result["status"] == "ELIGIBLE_FOR_PREDEPLOY_REVIEW"
    assert (result["ci_run_id"], result["image_run_id"]) == (1, 2)


@pytest.mark.parametrize("change", ["cancelled", "failure", "skipped", None])
def test_later_invalid_ci_does_not_erase_prior_exact_sha_success(change: str | None) -> None:
    ci, images = success()
    ci.append(run(release.CI_WORKFLOW, "push", 3, conclusion=change))
    assert release.verify_evidence(SHA, SHA, ci, images)["ci_run_id"] == 1


@pytest.mark.parametrize("change", ["cancelled", "failure", None])
def test_later_rejected_image_attempt_does_not_erase_published_sha(
    change: str | None,
) -> None:
    ci, images = success()
    images.append(run(release.IMAGES_WORKFLOW, "workflow_run", 4, conclusion=change))
    assert release.verify_evidence(SHA, SHA, ci, images)["image_run_id"] == 2


def test_rejected_runs_cannot_make_release_eligible() -> None:
    ci, images = success()
    ci[0]["conclusion"] = "failure"
    with pytest.raises(release.ReleaseNotEligible):
        release.verify_evidence(SHA, SHA, ci, images)
    ci[0]["conclusion"] = "success"
    images[0]["conclusion"] = "failure"
    with pytest.raises(release.ReleaseNotEligible):
        release.verify_evidence(SHA, SHA, ci, images)


def test_stale_main_blocks() -> None:
    ci, images = success()
    with pytest.raises(release.ReleaseNotEligible):
        release.verify_evidence(SHA, "b" * 40, ci, images)


@pytest.mark.parametrize(
    "changes",
    [
        {"head_sha": "b" * 40},
        {"head_branch": "not-main"},
        {"event": "pull_request"},
        {"path": "other.yml"},
        {"repository": {"full_name": "attacker/katcha"}},
    ],
)
def test_foreign_or_unrelated_ci_cannot_attest(changes: dict) -> None:
    ci, images = success()
    ci[0].update(changes)
    with pytest.raises(release.ReleaseNotEligible):
        release.verify_evidence(SHA, SHA, ci, images)


def test_image_run_before_ci_blocks() -> None:
    ci, images = success()
    images[0]["run_started_at"] = "2026-10-13T14:59:00Z"
    with pytest.raises(release.ReleaseNotEligible):
        release.verify_evidence(SHA, SHA, ci, images)


def test_missing_or_incomplete_images_block() -> None:
    ci, images = success()
    images[0]["status"] = "in_progress"
    with pytest.raises(release.ReleaseNotEligible):
        release.verify_evidence(SHA, SHA, ci, images)
    with pytest.raises(release.ReleaseNotEligible):
        release.verify_evidence(SHA, SHA, ci, [])


def test_invalid_sha_blocks() -> None:
    ci, images = success()
    with pytest.raises(release.ReleaseNotEligible):
        release.verify_evidence("main", SHA, ci, images)


def test_api_rejects_untrusted_relative_path() -> None:
    for path in ("https://example.org", "/../secrets", "//evil.org"):
        with pytest.raises(release.ReleaseNotEligible):
            release.api_get(path)


@pytest.mark.parametrize("field", ["updated_at", "run_started_at"])
@pytest.mark.parametrize("bad", [None, 123, "", "a", "z", "2026-10-13T15:01:00",
                                "2026-99-13T15:01:00Z"])
def test_malformed_or_naive_timestamps_block(field: str, bad: object) -> None:
    ci, images = success()
    target = ci[0] if field == "updated_at" else images[0]
    target[field] = bad
    with pytest.raises(release.ReleaseNotEligible):
        release.verify_evidence(SHA, SHA, ci, images)


def test_timestamp_offsets_use_absolute_time() -> None:
    ci, images = success()
    ci[0]["updated_at"] = "2026-10-13T16:01:00+01:00"
    images[0]["run_started_at"] = "2026-10-13T15:02:00Z"
    assert release.verify_evidence(SHA, SHA, ci, images)["sha"] == SHA
    ci[0]["updated_at"] = "2026-10-13T14:01:00-01:00"
    images[0]["run_started_at"] = "2026-10-13T14:02:00Z"
    with pytest.raises(release.ReleaseNotEligible):
        release.verify_evidence(SHA, SHA, ci, images)


@pytest.mark.parametrize("changes", [{"repository": None}, {"repository": []},
                                    {"id": "invalid"}, {"id": True}, {"id": 0}])
def test_malformed_run_fields_block(changes: dict) -> None:
    ci, images = success()
    ci[0].update(changes)
    with pytest.raises(release.ReleaseNotEligible):
        release.verify_evidence(SHA, SHA, ci, images)


@pytest.mark.parametrize("main", [{"commit": None}, {"commit": []},
                                 {"commit": {"sha": None}}])
def test_malformed_main_response_blocks(monkeypatch: pytest.MonkeyPatch, main: dict) -> None:
    monkeypatch.setattr(release, "api_get", lambda path: main)
    with pytest.raises(release.ReleaseNotEligible):
        release.check_release(SHA)


@pytest.mark.parametrize("result", [
    {"total_count": 0, "workflow_runs": None},
    {"total_count": None, "workflow_runs": []},
    {"total_count": "1", "workflow_runs": []},
    {"total_count": True, "workflow_runs": []},
    {"total_count": 1, "workflow_runs": []},
])
def test_malformed_or_truncated_run_list_blocks(
    monkeypatch: pytest.MonkeyPatch, result: dict
) -> None:
    def fetch(path: str) -> dict:
        return {"commit": {"sha": SHA}} if path == "/branches/main" else result
    monkeypatch.setattr(release, "api_get", fetch)
    with pytest.raises(release.ReleaseNotEligible):
        release.check_release(SHA)
