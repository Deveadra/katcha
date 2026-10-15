"""Exact release eligibility must distinguish inert scheduled runs from real builds."""
from __future__ import annotations

import pytest

from katcha.ops import release_eligibility as release

SHA = "a" * 40


def run(path: str, event: str, ident: int, **updates: object) -> dict:
    value = {
        "path": path,
        "event": event,
        "id": ident,
        "head_sha": SHA,
        "head_branch": "main",
        "repository": {"full_name": release.REPOSITORY},
        "status": "completed",
        "conclusion": "success",
        "updated_at": "2026-10-15T10:00:00Z",
        "run_started_at": "2026-10-15T10:10:00Z",
    }
    value.update(updates)
    return value


def jobs(*, skipped: bool = False, broken: str | None = None) -> dict:
    names = [
        release.PLAN_JOB,
        *[f"{name} (linux/amd64,linux/arm64)" for name in sorted(release.PUBLISHED_IMAGES)],
        release.VERIFY_JOB,
    ]
    rows = [
        {
            "name": name,
            "status": "completed",
            "conclusion": "skipped" if skipped else "success",
        }
        for name in names
    ]
    if broken:
        for row in rows:
            if row["name"] == broken:
                row["conclusion"] = "failure"
    return {"total_count": len(rows), "jobs": rows}


def mock_api(monkeypatch: pytest.MonkeyPatch, image_runs: list[dict], jobsets: dict[int, dict],
             *, ci_conclusion: str = "success") -> None:
    ci = run(release.CI_WORKFLOW, "push", 11, conclusion=ci_conclusion)
    ci["updated_at"] = "2026-10-15T09:59:00Z"

    def get(path: str):
        if path == "/branches/main":
            return {"commit": {"sha": SHA}}
        if path.startswith("/actions/workflows/ci.yml/runs?"):
            return {"total_count": 1, "workflow_runs": [ci]}
        if path.startswith("/actions/workflows/runtime-images.yml/runs?"):
            return {"total_count": len(image_runs), "workflow_runs": image_runs}
        for ident, result in jobsets.items():
            if path == f"/actions/runs/{ident}/jobs?per_page=100":
                return result
        pytest.fail(f"unexpected GitHub request {path}")

    monkeypatch.setattr(release, "api_get", get)


def test_older_verified_push_remains_eligible_after_skipped_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    normal = run(release.IMAGES_WORKFLOW, "workflow_run", 20)
    scheduled = run(release.IMAGES_WORKFLOW, "workflow_run", 21, conclusion="success")
    mock_api(monkeypatch, [scheduled, normal], {20: jobs(), 21: jobs(skipped=True)})
    result = release.check_release(SHA)
    assert result["image_run_id"] == 20
    assert result["ci_run_id"] == 11


@pytest.mark.parametrize("bad", [
    "failure", "cancelled", "in_progress",
])
def test_failed_real_build_never_falls_back_to_older_success(
    monkeypatch: pytest.MonkeyPatch, bad: str,
) -> None:
    earlier = run(release.IMAGES_WORKFLOW, "workflow_run", 20)
    newer = run(
        release.IMAGES_WORKFLOW, "workflow_run", 22,
        conclusion=bad if bad != "in_progress" else None,
        status="in_progress" if bad == "in_progress" else "completed",
    )
    mock_api(monkeypatch, [newer, earlier], {20: jobs(), 22: jobs()})
    with pytest.raises(release.ReleaseNotEligible):
        release.check_release(SHA)


def test_forged_workflow_success_without_anonymous_verification_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = run(release.IMAGES_WORKFLOW, "workflow_run", 22)
    mock_api(monkeypatch, [image], {
        22: jobs(broken=release.VERIFY_JOB),
    })
    with pytest.raises(release.ReleaseNotEligible, match="verification"):
        release.check_release(SHA)


def test_missing_sixth_build_blocks_even_if_workflow_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = run(release.IMAGES_WORKFLOW, "workflow_run", 22)
    build_jobs = jobs()
    build_jobs["jobs"].pop(1)
    build_jobs["total_count"] -= 1
    mock_api(monkeypatch, [image], {22: build_jobs})
    with pytest.raises(release.ReleaseNotEligible, match="six"):
        release.check_release(SHA)


def test_skipped_workflow_with_work_performed_is_not_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    older = run(release.IMAGES_WORKFLOW, "workflow_run", 20)
    later = run(release.IMAGES_WORKFLOW, "workflow_run", 22)
    suspicious = jobs(skipped=True)
    suspicious["jobs"][2]["conclusion"] = "success"
    mock_api(monkeypatch, [later, older], {20: jobs(), 22: suspicious})
    with pytest.raises(release.ReleaseNotEligible, match="unexpected work"):
        release.check_release(SHA)


@pytest.mark.parametrize("bad_jobs", [
    {"total_count": 9, "jobs": []},
    {"total_count": 8, "jobs": [{}]},
    {"total_count": "8", "jobs": [{}]},
    {"total_count": 0, "jobs": None},
])
def test_missing_or_truncated_job_evidence_blocks(
    monkeypatch: pytest.MonkeyPatch, bad_jobs: dict,
) -> None:
    image = run(release.IMAGES_WORKFLOW, "workflow_run", 22)
    mock_api(monkeypatch, [image], {22: bad_jobs})
    with pytest.raises(release.ReleaseNotEligible):
        release.check_release(SHA)


def test_ci_failure_blocks_even_with_successful_published_images(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = run(release.IMAGES_WORKFLOW, "workflow_run", 22)
    mock_api(monkeypatch, [image], {22: jobs()}, ci_conclusion="failure")
    with pytest.raises(release.ReleaseNotEligible):
        release.check_release(SHA)


def test_failed_image_plan_blocks_even_if_older_attempt_succeeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    older = run(release.IMAGES_WORKFLOW, "workflow_run", 20)
    later = run(release.IMAGES_WORKFLOW, "workflow_run", 22, conclusion="failure")
    bad = jobs()
    bad["jobs"][0]["conclusion"] = "failure"
    mock_api(monkeypatch, [later, older], {20: jobs(), 22: bad})
    with pytest.raises(release.ReleaseNotEligible, match="verification"):
        release.check_release(SHA)


def test_workflow_skips_non_push_upstream_without_disabling_pr_build() -> None:
    from pathlib import Path

    workflow = (
        Path(__file__).resolve().parents[1] / ".github/workflows/runtime-images.yml"
    ).read_text()
    assert "github.event_name == 'pull_request'" in workflow
    assert "github.event.workflow_run.event == 'push'" in workflow
    assert "github.event.workflow_run.conclusion == 'success'" in workflow
    assert "github.event.workflow_run.head_repository.full_name == github.repository" in workflow
    assert "core.setFailed" in workflow
