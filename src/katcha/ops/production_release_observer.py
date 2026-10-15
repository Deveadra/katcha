"""Read-only host-initiated Katcha production release observation.

A successful result permits review only. No Docker, systemd, OCI, migration,
fencing or production service mutation occurs here.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from katcha.ops import release_eligibility

SHA_RE = release_eligibility.SHA_RE


class ObserverError(ValueError):
    """Fail closed, with no secrets or raw provider responses in output."""


def read_pins(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ObserverError("production release configuration unavailable") from exc
    keys = {"KATCHA_RELEASE_SHA", "KATCHA_API_RELEASE_SHA"}
    values: dict[str, str] = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key = key.strip()
        if key not in keys:
            continue
        if not separator or key in values:
            raise ObserverError("release pin configuration malformed")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if value and not SHA_RE.fullmatch(value):
            raise ObserverError("release pin configuration malformed")
        values[key] = value
    if not SHA_RE.fullmatch(values.get("KATCHA_RELEASE_SHA", "")):
        raise ObserverError("production release pin missing or invalid")
    return values


def current_main(api_get: Callable[[str], Any]) -> str:
    result = api_get("/branches/main")
    if not isinstance(result, dict) or not isinstance(result.get("commit"), dict):
        raise ObserverError("main branch evidence malformed")
    sha = result["commit"].get("sha")
    if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
        raise ObserverError("main branch identity malformed")
    return sha


def compare_releases(api_get: Callable[[str], Any], old: str, new: str) -> dict[str, Any]:
    """Reject non-descendant, incomplete or oversized comparisons."""
    result = api_get(f"/compare/{old}...{new}")
    if not isinstance(result, dict):
        raise ObserverError("release comparison unavailable")
    merge_base = result.get("merge_base_commit")
    if not isinstance(merge_base, dict) or merge_base.get("sha") != old:
        raise ObserverError("candidate is not descended from configured release")
    if result.get("status") != "ahead" or result.get("behind_by") != 0:
        raise ObserverError("candidate does not advance the configured release")
    count = result.get("total_commits")
    files = result.get("files")
    if type(count) is not int or count <= 0 or count > 250:
        raise ObserverError("release commit comparison incomplete or too large")
    # GitHub compare caps changed files at 300; 300 could be truncated.
    if not isinstance(files, list) or not files or len(files) >= 300:
        raise ObserverError("release file comparison missing or truncated")
    paths: list[str] = []
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
            raise ObserverError("release changed-file identity malformed")
        name = item["filename"]
        if not name or name.startswith("/") or ".." in name.split("/") or chr(92) in name:
            raise ObserverError("release changed-file path malformed")
        if item.get("status") not in (
            "added", "modified", "removed", "renamed", "changed", "copied"
        ):
            raise ObserverError("release changed-file status malformed")
        paths.append(name)
    return {
        "commits": count,
        "changed_files": len(paths),
        "migration_changes": any(
            p.startswith("migrations/") or p == "alembic.ini" for p in paths
        ),
        "worker_changes": any(
            p.startswith(("src/katcha/orchestration/", "src/katcha/ops/",
                          "src/katcha/services/"))
            for p in paths
        ),
        "infrastructure_changes": any(
            p.startswith(("deploy/", "infra/", ".github/", "Dockerfile"))
            for p in paths
        ),
        "requires_supervised_compatibility_review": True,
    }


def inspect(
    env_file: Path,
    *,
    api_get: Callable[[str], Any] = release_eligibility.api_get,
    check_release: Callable[[str], dict[str, Any]] = release_eligibility.check_release,
) -> dict[str, Any]:
    pins = read_pins(env_file)
    configured = pins["KATCHA_RELEASE_SHA"]
    api_sha = pins.get("KATCHA_API_RELEASE_SHA") or configured
    latest = current_main(api_get)
    if latest == configured and api_sha == configured:
        return {"status": "CURRENT", "configured_sha": configured, "api_sha": api_sha}
    if latest == configured:
        return {
            "status": "REVIEW_REQUIRED",
            "configured_sha": configured,
            "api_sha": api_sha,
            "candidate_sha": latest,
            "reason": "API pin differs from global pin; supervised reconciliation needed",
        }
    try:
        evidence = check_release(latest)
    except release_eligibility.ReleaseNotEligible as exc:
        raise ObserverError("candidate release evidence not accepted: " + str(exc)) from exc
    if not isinstance(evidence, dict) or (
        evidence.get("status") != "ELIGIBLE_FOR_PREDEPLOY_REVIEW"
        or evidence.get("sha") != latest
        or type(evidence.get("ci_run_id")) is not int
        or type(evidence.get("image_run_id")) is not int
    ):
        raise ObserverError("release eligibility contract rejected")
    changes = compare_releases(api_get, configured, latest)
    if current_main(api_get) != latest:
        raise ObserverError("main advanced during release inspection")
    return {
        "status": "REVIEW_REQUIRED",
        "configured_sha": configured,
        "api_sha": api_sha,
        "candidate_sha": latest,
        "ci_run_id": evidence["ci_run_id"],
        "image_run_id": evidence["image_run_id"],
        "comparison": changes,
        "note": "Configured pins only, not running-image attestation; deployment disabled",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--production-env", type=Path, default=Path("/etc/katcha/katcha.env")
    )
    args = parser.parse_args()
    try:
        result = inspect(args.production_env)
    except (ObserverError, release_eligibility.ReleaseNotEligible) as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc)}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
