#!/usr/bin/env python3
"""Read-only production release eligibility. Never contacts OCI or changes a host.

An image tag existing is not sufficient evidence for an approved production
release. Both whole-workflow results must be successful for the exact current
main SHA. This check is intentionally separate from deployment authorization.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any

REPOSITORY = "Deveadra/katcha"
API = "https://api.github.com/repos/" + REPOSITORY
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
CI_WORKFLOW = ".github/workflows/ci.yml"
IMAGES_WORKFLOW = ".github/workflows/runtime-images.yml"


class ReleaseNotEligible(ValueError):
    """Reject an unproven release without mutating production."""


def api_get(path: str) -> Any:
    if not path.startswith("/") or "//" in path or ".." in path:
        raise ReleaseNotEligible("invalid GitHub API path")
    req = urllib.request.Request(
        API + path,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "Katcha-Release-Eligibility",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            return json.load(res)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError) as exc:
        # Do not leak redirects, tokens, response bodies or infrastructure data.
        raise ReleaseNotEligible("GitHub release evidence unavailable") from exc


def matching_runs(
    runs: list[dict[str, Any]], sha: str, workflow: str, event: str
) -> list[dict[str, Any]]:
    if not isinstance(runs, list):
        raise ReleaseNotEligible("missing workflow run list")
    matches: list[dict[str, Any]] = []
    for run in runs:
        if not isinstance(run, dict):
            raise ReleaseNotEligible("workflow run response malformed")
        repository = run.get("repository")
        if not isinstance(repository, dict):
            raise ReleaseNotEligible("workflow repository response malformed")
        ident = run.get("id")
        if type(ident) is not int or ident <= 0:
            raise ReleaseNotEligible("workflow run identity malformed")
        if (
            run.get("path") == workflow
            and run.get("head_sha") == sha
            and run.get("head_branch") == "main"
            and run.get("event") == event
            and repository.get("full_name") == REPOSITORY
        ):
            matches.append(run)
    return sorted(matches, key=lambda x: x["id"], reverse=True)



# Only a completed publication with all six built images and independent
# anonymous-pull verification is a deployable release. The image workflow
# also runs on scheduled/manual CI; those legitimate no-op executions must
# not invalidate the most recent verified push release of the same SHA.
PUBLISHED_IMAGES = {
    "katcha-control",
    "katcha-ingest",
    "katcha-analysis",
    "katcha-renderer",
    "katcha-production",
    "katcha-minio",
}
PLAN_JOB = "Plan runtime image builds"
VERIFY_JOB = "Verify anonymous runtime pulls"


def verified_publications(sha: str, image_runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Select the most recent real publication; never fall back after a failed build."""
    matches = matching_runs(image_runs, sha, IMAGES_WORKFLOW, "workflow_run")
    if not matches or len(matches) > 30:
        raise ReleaseNotEligible("publication evidence absent or exceeds bounded review")
    for run in matches:
        ident = run["id"]
        result = api_get(f"/actions/runs/{ident}/jobs?per_page=100")
        if not isinstance(result, dict):
            raise ReleaseNotEligible("publication job evidence malformed")
        jobs, total = result.get("jobs"), result.get("total_count")
        if (
            not isinstance(jobs, list) or not jobs or len(jobs) > 100
            or type(total) is not int or total != len(jobs)
        ):
            raise ReleaseNotEligible("publication job evidence missing or truncated")
        by_name: dict[str, dict[str, Any]] = {}
        for job in jobs:
            if not isinstance(job, dict) or not isinstance(job.get("name"), str):
                raise ReleaseNotEligible("publication job identity malformed")
            name = job["name"]
            if name in by_name or job.get("status") != "completed":
                raise ReleaseNotEligible("publication job incomplete or duplicated")
            by_name[name] = job
        plan, verify = by_name.get(PLAN_JOB), by_name.get(VERIFY_JOB)
        if not plan or not verify:
            raise ReleaseNotEligible("publication verification jobs missing")
        if plan.get("conclusion") == "skipped":
            # Only a fully inert workflow_run (e.g. scheduled CI) is ignored.
            # A real build/verification failure must block; never fall back.
            if (
                run.get("status") == "completed"
                and run.get("conclusion") in ("success", "skipped")
                and all(job.get("conclusion") == "skipped" for job in jobs)
            ):
                continue
            raise ReleaseNotEligible("skipped publication contains unexpected work")
        if (
            plan.get("conclusion") != "success"
            or run.get("status") != "completed"
            or run.get("conclusion") != "success"
            or verify.get("conclusion") != "success"
        ):
            raise ReleaseNotEligible("latest image publication failed verification")
        builds: set[str] = set()
        for name, job in by_name.items():
            if name in (PLAN_JOB, VERIFY_JOB):
                continue
            prefix = name.split(" (", 1)[0]
            if (
                prefix not in PUBLISHED_IMAGES or prefix in builds
                or job.get("conclusion") != "success"
                or not name.endswith(")")
            ):
                raise ReleaseNotEligible("production image build evidence incomplete")
            builds.add(prefix)
        if builds != PUBLISHED_IMAGES:
            raise ReleaseNotEligible("not all six runtime images were verified")
        return [run]
    raise ReleaseNotEligible("no completed image publication for the current revision")

def evidence_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ReleaseNotEligible("workflow timestamp malformed")
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReleaseNotEligible("workflow timestamp malformed") from exc
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ReleaseNotEligible("workflow timestamp must include timezone")
    return timestamp


def verify_evidence(
    sha: str, main_sha: str, ci_runs: list[dict[str, Any]],
    image_runs: list[dict[str, Any]]
) -> dict[str, Any]:
    if (
        not isinstance(sha, str) or not SHA_RE.fullmatch(sha)
        or not isinstance(main_sha, str) or not SHA_RE.fullmatch(main_sha)
    ):
        raise ReleaseNotEligible("invalid commit identity")
    if sha != main_sha:
        raise ReleaseNotEligible("release SHA differs from current main")
    ci = matching_runs(ci_runs, sha, CI_WORKFLOW, "push")
    images = matching_runs(image_runs, sha, IMAGES_WORKFLOW, "workflow_run")
    if not ci or not images:
        raise ReleaseNotEligible("matching CI and image publication runs not found")
    last_ci, last_image = ci[0], images[0]
    if any(
        run.get("status") != "completed" or run.get("conclusion") != "success"
        for run in (last_ci, last_image)
    ):
        raise ReleaseNotEligible("latest matching run not successful")
    # A workflow_run normally starts after CI finishes. Reject unrelated or
    # reordered successes rather than inferring release authority.
    ci_finished = evidence_timestamp(last_ci.get("updated_at"))
    image_started = evidence_timestamp(last_image.get("run_started_at"))
    if image_started < ci_finished:
        raise ReleaseNotEligible("image publication does not follow successful CI")
    return {
        "status": "ELIGIBLE_FOR_PREDEPLOY_REVIEW",
        "sha": sha,
        "ci_run_id": last_ci["id"],
        "image_run_id": last_image["id"],
        "warning": "Not a deployment authorization; no OCI state inspected or modified.",
    }


def check_release(sha: str) -> dict[str, Any]:
    if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
        raise ReleaseNotEligible("sha must be an exact lowercase 40-character git commit")
    main = api_get("/branches/main")
    if not isinstance(main, dict):
        raise ReleaseNotEligible("invalid main branch response")
    commit = main.get("commit")
    if not isinstance(commit, dict):
        raise ReleaseNotEligible("invalid main commit response")
    current = commit.get("sha")
    if not isinstance(current, str) or not SHA_RE.fullmatch(current):
        raise ReleaseNotEligible("invalid main commit identity")
    # Both sources are fetched independently, scoped by their named workflow.
    # Pagination is deliberately bounded and all failure paths are fail-closed.
    params = urllib.parse.urlencode({"head_sha": sha, "per_page": 100})
    ci_result = api_get("/actions/workflows/ci.yml/runs?" + params)
    image_result = api_get("/actions/workflows/runtime-images.yml/runs?" + params)
    if not isinstance(ci_result, dict) or not isinstance(image_result, dict):
        raise ReleaseNotEligible("workflow evidence response malformed")
    for result in (ci_result, image_result):
        runs = result.get("workflow_runs")
        count = result.get("total_count")
        if not isinstance(runs, list):
            raise ReleaseNotEligible("missing workflow run list")
        if type(count) is not int or count < 0 or count < len(runs):
            raise ReleaseNotEligible("workflow evidence count malformed")
        if count > len(runs):
            raise ReleaseNotEligible("workflow evidence truncated; refuse to infer approval")
    return verify_evidence(
        sha, current, ci_result["workflow_runs"],
        verified_publications(sha, image_result["workflow_runs"])
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sha", help="exact 40-character candidate commit SHA")
    args = parser.parse_args()
    try:
        outcome = check_release(args.sha)
    except ReleaseNotEligible as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc)}))
        return 2
    print(json.dumps(outcome, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
