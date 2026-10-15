"""Read-only attestation of the actual OCI production Compose runtime.

This is an observation and fail-closed guard, NOT a rollout executor. It never
starts/stops containers, changes release pins, or invokes the OCI recovery API.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from katcha.ops import production_release_observer as observer
from katcha.ops.production_release_observer import read_pins

PROJECT = "katcha-production"
REGISTRY = "ghcr.io/deveadra"
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
IMAGE_ID_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
CONTAINER_ID_RE = re.compile(r"[0-9a-f]{12,64}\Z")
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
HEALTHCHECKED = {"postgres", "temporal", "api", "renderer"}
SERVICES: dict[str, str] = {
    "postgres": "postgres:16-alpine",
    "temporal": "temporalio/auto-setup:1.27.2",
    "api": "katcha-control",
    "cloudflared": "cloudflare/cloudflared:2026.9.2",
    "worker": "katcha-ingest",
    "intelligence-worker": "katcha-control",
    "renderer": "katcha-renderer",
    "production-worker": "katcha-production",
    "telegram-worker": "katcha-control",
}


class PreflightBlocked(ValueError):
    """Unexpected or unverified read-only host observation."""


def host_command(*args: str) -> str:
    """Strict allowlist of read-only operations; never echo command errors."""
    permitted = (
        args == ("systemctl", "is-active", "katcha.service")
        or args == (
            "systemctl", "show", "--property=MainPID", "--value", "katcha.service"
        )
        or args == (
            "docker", "ps", "--filter", f"label=com.docker.compose.project={PROJECT}",
            "--format", "{{.ID}}",
        )
        or (
            len(args) > 2 and args[:2] == ("docker", "inspect")
            and all(CONTAINER_ID_RE.fullmatch(item) for item in args[2:])
        )
        or (
            len(args) > 3 and args[:3] == ("docker", "image", "inspect")
            and all(IMAGE_ID_RE.fullmatch(item) for item in args[3:])
        )
    )
    if not permitted:
        raise PreflightBlocked("unsupported host observation")
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, check=False, timeout=15
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PreflightBlocked("host observation failed") from exc
    if result.returncode != 0 or len(result.stdout) > 1_000_000:
        raise PreflightBlocked("host observation unsuccessful")
    return result.stdout.strip()


def local_json(url: str) -> dict[str, Any]:
    """Fixed-address HTTP observations; no untrusted URLs or secret headers."""
    if url not in {
        "http://127.0.0.1:8000/v1/health/ready",
        "http://127.0.0.1:8000/v1/health/release",
    }:
        raise PreflightBlocked("unsupported local health observation")
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            if response.status != 200:
                raise PreflightBlocked("local API is not ready")
            data = response.read(8193)
            if len(data) > 8192:
                raise PreflightBlocked("unexpected local API response")
        parsed = json.loads(data)
    except (OSError, UnicodeError, ValueError, urllib.error.URLError) as exc:
        raise PreflightBlocked("local API health unavailable") from exc
    if not isinstance(parsed, dict):
        raise PreflightBlocked("local API health response malformed")
    return parsed


def _json_list(raw: str, label: str) -> list[dict[str, Any]]:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise PreflightBlocked(f"{label} inventory malformed") from exc
    if not isinstance(value, list) or not value or len(value) > 40:
        raise PreflightBlocked(f"{label} inventory missing or oversized")
    if any(not isinstance(row, dict) for row in value):
        raise PreflightBlocked(f"{label} inventory malformed")
    return value


def _expected_ref(service: str, global_sha: str, api_sha: str) -> str:
    image = SERVICES[service]
    if service in {"postgres", "temporal", "cloudflared"}:
        return image
    return f"{REGISTRY}/{image}:{api_sha if service == 'api' else global_sha}"


def inspect(
    env_file: Path,
    *,
    command: Callable[..., str] = host_command,
    fetch: Callable[[str], dict[str, Any]] = local_json,
) -> dict[str, Any]:
    """Attest the active containers; no OCI leader or DB migration approval."""
    pins = read_pins(env_file)
    global_sha = pins["KATCHA_RELEASE_SHA"]
    api_sha = pins.get("KATCHA_API_RELEASE_SHA") or global_sha
    if not SHA_RE.fullmatch(global_sha) or not SHA_RE.fullmatch(api_sha):
        raise PreflightBlocked("configured image pin malformed")

    if command("systemctl", "is-active", "katcha.service") != "active":
        raise PreflightBlocked("production supervisor not active")
    pid = command("systemctl", "show", "--property=MainPID", "--value", "katcha.service")
    if not pid.isdecimal() or int(pid) <= 1:
        raise PreflightBlocked("production supervisor has no active owner")

    output = command(
        "docker", "ps", "--filter", f"label=com.docker.compose.project={PROJECT}",
        "--format", "{{.ID}}",
    )
    ids = output.splitlines()
    if len(ids) != len(SERVICES) or len(set(ids)) != len(ids):
        raise PreflightBlocked("unexpected production container count")
    if any(not CONTAINER_ID_RE.fullmatch(cid) for cid in ids):
        raise PreflightBlocked("production container identity malformed")
    rows = _json_list(command("docker", "inspect", *ids), "container")
    seen_ids = [row.get("Id") for row in rows]
    if (
        len(seen_ids) != len(ids)
        or any(not isinstance(cid, str) for cid in seen_ids)
        or not all(any(cid.startswith(prefix) for cid in seen_ids) for prefix in ids)
        or len(set(seen_ids)) != len(ids)
    ):
        raise PreflightBlocked("container inventory identity mismatch")
    if len(rows) != len(ids):
        raise PreflightBlocked("incomplete production container inventory")

    image_ids: dict[str, set[str]] = {}
    observations: dict[str, dict[str, str]] = {}
    for row in rows:
        config, state = row.get("Config"), row.get("State")
        if not isinstance(config, dict) or not isinstance(state, dict):
            raise PreflightBlocked("production container metadata malformed")
        labels = config.get("Labels")
        if not isinstance(labels, dict) or labels.get("com.docker.compose.project") != PROJECT:
            raise PreflightBlocked("unexpected production container ownership")
        service = labels.get("com.docker.compose.service")
        if service not in SERVICES or service in observations:
            raise PreflightBlocked("duplicate or unexpected production service")
        if state.get("Running") is not True or state.get("Status") != "running":
            raise PreflightBlocked("production service is not running")
        health = state.get("Health")
        if service in HEALTHCHECKED and health is None:
            raise PreflightBlocked("required service healthcheck missing")
        if health is not None and (
            not isinstance(health, dict) or health.get("Status") != "healthy"
        ):
            raise PreflightBlocked("production service is not healthy")
        image_ref = config.get("Image")
        if image_ref != _expected_ref(service, global_sha, api_sha):
            raise PreflightBlocked("container image differs from configured release")
        local_image_id = row.get("Image")
        if not isinstance(local_image_id, str) or not IMAGE_ID_RE.fullmatch(local_image_id):
            raise PreflightBlocked("running container image ID invalid")
        image_ids.setdefault(local_image_id, set()).add(service)
        if service not in {"postgres", "temporal", "cloudflared"}:
            expected_sha = api_sha if service == "api" else global_sha
            if labels.get("org.opencontainers.image.revision") != expected_sha:
                raise PreflightBlocked("running container revision label mismatch")
        observations[service] = {
            "image": image_ref,
            "image_id": local_image_id,
            "health": "healthy" if health is not None else "running-no-healthcheck",
        }
    if set(observations) != set(SERVICES):
        raise PreflightBlocked("required production service missing")

    images = _json_list(command("docker", "image", "inspect", *sorted(image_ids)), "image")
    if len(images) != len(image_ids):
        raise PreflightBlocked("incomplete production image inventory")
    verified_images: set[str] = set()
    for image in images:
        local_id = image.get("Id")
        if local_id not in image_ids or local_id in verified_images:
            raise PreflightBlocked("production image inventory mismatch")
        if image.get("Os") != "linux" or image.get("Architecture") != "arm64":
            raise PreflightBlocked("OCI production image architecture mismatch")
        verified_images.add(local_id)
        services = image_ids[local_id]
        if services & {"postgres", "temporal", "cloudflared"}:
            # Third-party images cannot inherit our Katcha revision labels.
            if any(service not in {"postgres", "temporal", "cloudflared"} for service in services):
                raise PreflightBlocked("third-party and application image ID collision")
            continue
        config = image.get("Config")
        if not isinstance(config, dict) or not isinstance(config.get("Labels"), dict):
            raise PreflightBlocked("application image metadata missing")
        expected = {api_sha if name == "api" else global_sha for name in services}
        if (
            len(expected) != 1
            or config["Labels"].get("org.opencontainers.image.revision") not in expected
        ):
            raise PreflightBlocked("application image revision mismatch")
        repo_digests = image.get("RepoDigests")
        if not isinstance(repo_digests, list) or not repo_digests:
            raise PreflightBlocked("application registry digest unavailable")
        for service in services:
            name = SERVICES[service]
            prefix = f"{REGISTRY}/{name}@"
            matches = [
                value for value in repo_digests
                if isinstance(value, str) and value.startswith(prefix)
                and DIGEST_RE.fullmatch(value[len(prefix):])
            ]
            if not matches:
                raise PreflightBlocked("application registry provenance not observed")
            observations[service]["registry_digest"] = matches[0]
    if verified_images != set(image_ids):
        raise PreflightBlocked("unverified production image identity")

    ready = fetch("http://127.0.0.1:8000/v1/health/ready")
    if ready.get("status") != "ok":
        raise PreflightBlocked("local API readiness invalid")
    release = fetch("http://127.0.0.1:8000/v1/health/release")
    if release.get("release_sha") != api_sha:
        raise PreflightBlocked("API release identity differs from actual configured image")
    return {
        "status": "ATTESTED_FOR_REVIEW",
        "global_sha": global_sha,
        "api_sha": api_sha,
        "supervisor": "active",
        "service_count": len(observations),
        "services": {name: observations[name] for name in sorted(observations)},
        "limitations": [
            "Configured release and local runtime only; not an OCI leadership/fence check",
            "Local registry digest presence is not remote digest equality verification",
            "Database migrations, Temporal compatibility and R2 backups not verified",
            "No production deployment is authorized or performed",
        ],
    }


def assess_candidate(
    env_file: Path,
    candidate_sha: str,
    *,
    command: Callable[..., str] = host_command,
    fetch: Callable[[str], dict[str, Any]] = local_json,
    inspect_release: Callable[..., dict[str, Any]] = observer.inspect,
) -> dict[str, Any]:
    """Combine host truth with CI/release observation; still never authorizes deploy."""
    if not SHA_RE.fullmatch(candidate_sha):
        raise PreflightBlocked("candidate revision must be an exact git SHA")
    host = inspect(env_file, command=command, fetch=fetch)
    try:
        eligibility = inspect_release(env_file)
    except ValueError as exc:
        raise PreflightBlocked("release eligibility observation blocked") from exc
    if not isinstance(eligibility, dict) or eligibility.get("candidate_sha") != candidate_sha:
        raise PreflightBlocked("release candidate does not match eligible main")
    if eligibility.get("status") != "REVIEW_REQUIRED" or not isinstance(
        eligibility.get("comparison"), dict
    ):
        raise PreflightBlocked("candidate eligibility comparison missing")
    if host["api_sha"] != host["global_sha"]:
        raise PreflightBlocked("mixed API and worker pins require supervised reconciliation")
    return {
        "status": "REVIEW_REQUIRED",
        "candidate_sha": candidate_sha,
        "ci_run_id": eligibility["ci_run_id"],
        "image_run_id": eligibility["image_run_id"],
        "comparison": eligibility["comparison"],
        "host": host,
        "warning": "No migration/Temporal/fencing/backup authorization. No deployment performed.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--production-env", type=Path, default=Path("/etc/katcha/katcha.env")
    )
    parser.add_argument(
        "--candidate-sha", help="optional approved main candidate for review, not deployment"
    )
    args = parser.parse_args()
    try:
        if args.candidate_sha:
            result = assess_candidate(args.production_env, args.candidate_sha)
        else:
            result = inspect(args.production_env)
    except (PreflightBlocked, ValueError) as exc:
        # Never echo command output, environment values or arbitrary provider errors.
        reason = str(exc) if isinstance(exc, PreflightBlocked) else "release configuration invalid"
        print(json.dumps({"status": "BLOCKED", "reason": reason}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
