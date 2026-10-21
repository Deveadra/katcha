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


def test_verified_release_survives_newer_main_merges(monkeypatch: pytest.MonkeyPatch) -> None:
    newer_main = "b" * 40
    ci, images = success()
    comparison_path = f"/compare/{SHA}...{newer_main}"

    def fetch(path: str) -> dict:
        if path == "/branches/main":
            return {"commit": {"sha": newer_main}}
        if path == comparison_path:
            return {
                "status": "ahead", "behind_by": 0,
                "merge_base_commit": {"sha": SHA},
            }
        if path.startswith("/actions/workflows/ci.yml/runs?"):
            return {"workflow_runs": ci, "total_count": 1}
        if path.startswith("/actions/workflows/runtime-images.yml/runs?"):
            return {"workflow_runs": images, "total_count": 1}
        pytest.fail(f"unexpected API request: {path}")

    monkeypatch.setattr(release, "api_get", fetch)
    monkeypatch.setattr(release, "verified_publications", lambda *args: images)
    result = release.check_release(SHA)
    assert result["sha"] == SHA
    assert result["ci_run_id"] == 1
    assert result["image_run_id"] == 2


@pytest.mark.parametrize("proof", [
    {"status": "diverged", "behind_by": 1, "merge_base_commit": {"sha": SHA}},
    {"status": "ahead", "behind_by": 0, "merge_base_commit": {"sha": "c" * 40}},
    {"status": "ahead", "behind_by": 0, "merge_base_commit": None},
    {"status": "behind", "behind_by": 1, "merge_base_commit": {"sha": SHA}},
    {},
])
def test_unaccepted_release_history_blocks(
    monkeypatch: pytest.MonkeyPatch, proof: dict,
) -> None:
    monkeypatch.setattr(release, "api_get", lambda path: proof)
    with pytest.raises(release.ReleaseNotEligible, match="not an accepted main ancestor"):
        release.accepted_main_ancestry(SHA, "b" * 40)


def test_exact_current_main_needs_no_compare(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(release, "api_get", lambda path: pytest.fail(path))
    release.accepted_main_ancestry(SHA, SHA)


def test_compare_endpoint_requires_two_immutable_shas(monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    seen = []
    def open_response(request, timeout):
        seen.append(request.full_url)
        return io.StringIO("{}")
    monkeypatch.setattr(release.urllib.request, "urlopen", open_response)
    assert release.api_get(f"/compare/{SHA}...{'b'*40}") == {}
    assert seen == [release.API + f"/compare/{SHA}...{'b'*40}"]
    for path in (f"/compare/{SHA}...main", f"/compare/{SHA}...{'b'*40}/../x"):
        with pytest.raises(release.ReleaseNotEligible, match="invalid GitHub API path"):
            release.api_get(path)


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
