"""Fail-closed contracts for release evidence, without network or OCI calls."""
from __future__ import annotations

import pytest

from scripts import check_production_release as release

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
def test_latest_ci_non_success_blocks(change: str | None) -> None:
    ci, images = success()
    ci.append(run(release.CI_WORKFLOW, "push", 3, conclusion=change))
    with pytest.raises(release.ReleaseNotEligible):
        release.verify_evidence(SHA, SHA, ci, images)


@pytest.mark.parametrize("change", ["cancelled", "failure", None])
def test_latest_image_non_success_blocks(change: str | None) -> None:
    ci, images = success()
    images.append(run(release.IMAGES_WORKFLOW, "workflow_run", 4, conclusion=change))
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
