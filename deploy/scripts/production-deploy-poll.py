#!/usr/bin/env python3
"""Host-initiated receipt consumer for reviewed GitHub production deployments.

No inbound network connection, GitHub token or remote code execution. A
short-lived HMAC request grants permission to run one fixed root-owned executor.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

API = ("https://api.github.com/repos/Deveadra/katcha/deployments"
       "?environment=katcha-production&per_page=30")
HOST = "katcha-prod-primary-001"
KEY_PATH = Path("/etc/katcha/production-deploy.key")
GATE = Path("/etc/katcha/production-deploy.enabled")
DATA = Path("/srv/katcha/deployments")
RECEIPTS = DATA / "requests"
EXECUTOR = Path("/usr/local/sbin/katcha-production-deploy")
SHA = re.compile(r"[0-9a-f]{40}\Z")
HEX32 = re.compile(r"[0-9a-f]{32}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")


class RequestBlocked(ValueError):
    """Unsigned, expired, malformed or incorrectly addressed deployment request."""


def private_root_file(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise RequestBlocked("required root-only deployment authorization absent")
    stat = path.stat()
    if stat.st_uid != 0 or stat.st_mode & 0o077:
        raise RequestBlocked("deployment authorization ownership/mode invalid")
    try:
        return path.read_bytes().strip()
    except OSError as exc:
        raise RequestBlocked("deployment authorization unavailable") from exc


def key_from_file(path: Path = KEY_PATH) -> bytes:
    text = private_root_file(path).decode("ascii")
    if not HEX64.fullmatch(text):
        raise RequestBlocked("deployment signing key must be 32-byte lowercase hex")
    return bytes.fromhex(text)


def canonical(payload: dict[str, Any]) -> bytes:
    return "\n".join((
        "katcha-production-v2", payload["sha"], payload["policy"],
        payload["migration_policy"], payload["host"],
        str(payload["issued_at"]), str(payload["expires_at"]), payload["nonce"],
    )).encode("utf-8")


def authorized_request(
    deployment: dict[str, Any], key: bytes, now: int,
) -> dict[str, Any]:
    ident = deployment.get("id")
    raw = deployment.get("payload")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise RequestBlocked("deployment payload malformed") from exc
    if (type(ident) is not int or ident <= 0 or not isinstance(raw, dict)
        or deployment.get("environment") != "katcha-production"
        or deployment.get("task") != "deploy"):
        raise RequestBlocked("deployment request identity invalid")
    payload = raw
    if set(payload) != {
        "version", "sha", "policy", "migration_policy", "host",
        "issued_at", "expires_at",
        "nonce", "signature",
    }:
        raise RequestBlocked("deployment request fields unexpected")
    if (
        type(payload["version"]) is not int or payload["version"] != 2
        or not isinstance(payload["sha"], str) or not SHA.fullmatch(payload["sha"])
        or payload["sha"] != deployment.get("sha")
        or not isinstance(payload["policy"], str)
        or payload["policy"] not in {"refuse", "approved-resume"}
        or not isinstance(payload["migration_policy"], str)
        or payload["migration_policy"] not in {
            "refuse", "approve-additive-0064", "approve-additive-through-0065", "approve-additive-through-0066", "approve-additive-through-0067",
        }
        or payload["host"] != HOST
        or type(payload["issued_at"]) is not int
        or type(payload["expires_at"]) is not int
        or type(payload["nonce"]) is not str or not HEX32.fullmatch(payload["nonce"])
        or type(payload["signature"]) is not str
        or not HEX64.fullmatch(payload["signature"])
    ):
        raise RequestBlocked("deployment request assertions invalid")
    if (payload["issued_at"] > now + 60
        or payload["expires_at"] < now
        or payload["expires_at"] - payload["issued_at"] != 1200):
        raise RequestBlocked("deployment request expired or time window invalid")
    signature = hmac.new(key, canonical(payload), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, payload["signature"]):
        raise RequestBlocked("deployment signature invalid")
    return {"id": ident, "sha": payload["sha"], "policy": payload["policy"],
            "migration_policy": payload["migration_policy"],
            "nonce": payload["nonce"]}


def fetch_requests() -> list[dict[str, Any]]:
    req = urllib.request.Request(
        API, headers={"User-Agent": "Katcha-Production-Host-Poll",
                      "Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            raw = response.read(1_000_001)
        if len(raw) > 1_000_000:
            raise RequestBlocked("deployment request response oversized")
        records = json.loads(raw)
    except (OSError, urllib.error.URLError, ValueError) as exc:
        raise RequestBlocked("GitHub deployment request feed unavailable") from exc
    if not isinstance(records, list) or len(records) > 30:
        raise RequestBlocked("deployment request feed malformed")
    return records


def secure_receipts() -> None:
    if not DATA.parent.is_mount() or os.geteuid() != 0:
        raise RequestBlocked("durable production mount or root authority missing")
    for path in (DATA, RECEIPTS):
        if path.is_symlink():
            raise RequestBlocked("deployment receipt path symlinked")
        path.mkdir(mode=0o700, exist_ok=True)
        stat = path.stat()
        if stat.st_uid != 0 or stat.st_mode & 0o077:
            raise RequestBlocked("deployment receipt directory not root-private")


def executor_attestation(returncode: int, stdout: str, sha: str) -> str | None:
    """A zero exit code is not evidence of a successfully deployed release."""
    if returncode != 0 or not isinstance(stdout, str) or len(stdout) > 16384:
        return None
    try:
        outcome = json.loads(stdout)
    except (ValueError, TypeError, RecursionError):
        return None
    if (not isinstance(outcome, dict) or outcome.get("sha") != sha
        or outcome.get("status") not in {"DEPLOYED", "ALREADY_CURRENT"}):
        return None
    return outcome["status"]



EXECUTOR_FAILURE_REASONS = {
    "HOST_DISK_FULL": "Host storage is full; review image capacity before a new request.",
    "HOST_OPERATION_FAILED": "A host operation failed before deployment completed.",
    "HOST_OPERATION_TIMEOUT": "A host operation did not start or timed out.",
    "DEPLOYMENT_GUARD_REJECTED": "A deployment guard rejected the release.",
    "TEMPORAL_DRAIN_REQUIRED": "Pending Temporal work must drain before a new request.",
    "DEPLOYMENT_LOCKED": "Another production deployment owns the executor lock.",
    "DEPLOYMENT_NOT_AUTHORIZED": "The host deployment gate did not authorize this attempt.",
    "RELEASE_EVIDENCE_REJECTED": "Independent release evidence was rejected.",
    "EXECUTOR_ERROR": "The executor encountered an unclassified local error.",
    "EXECUTOR_TIMEOUT": "The fixed executor exceeded its deadline.",
    "EXECUTOR_UNAVAILABLE": "The fixed root-owned executor could not be started safely.",
    "EXECUTOR_OUTPUT_UNVERIFIED": "The executor did not attest a successful exact release.",
    "EXECUTOR_REJECTION_UNCLASSIFIED": "The executor failed without a safe structured reason.",
}
EXECUTOR_OPERATIONS = {"unknown", "docker.pull", "docker.inspect", "docker.exec", "docker.ps",
                       "docker.run", "docker.image", "docker.other", "git", "systemctl",
                       "mountpoint", "python3", "host.other"}


def failure_diagnostic(code: str, operation: str = "unknown") -> dict[str, str]:
    return {"code": code, "operation": operation, "reason": EXECUTOR_FAILURE_REASONS[code]}


def executor_rejection(returncode: int, stdout: str, sha: str) -> dict[str, str]:
    """Accept an exact, bounded enum-only rejection; never preserve raw process output."""
    fallback = failure_diagnostic("EXECUTOR_OUTPUT_UNVERIFIED" if returncode == 0 else
                                  "EXECUTOR_REJECTION_UNCLASSIFIED")
    if returncode == 0 or not isinstance(stdout, str) or len(stdout) > 2048:
        return fallback
    try:
        outcome = json.loads(stdout)
    except (ValueError, TypeError, RecursionError):
        return fallback
    if (not isinstance(outcome, dict) or set(outcome) != {"status", "sha", "diagnostic"}
        or outcome["status"] != "BLOCKED" or outcome["sha"] != sha):
        return fallback
    diagnostic = outcome["diagnostic"]
    if (not isinstance(diagnostic, dict) or set(diagnostic) != {"code", "operation"}
        or not isinstance(diagnostic["code"], str)
        or diagnostic["code"] not in EXECUTOR_FAILURE_REASONS
        or not isinstance(diagnostic["operation"], str)
        or diagnostic["operation"] not in EXECUTOR_OPERATIONS):
        return fallback
    return failure_diagnostic(diagnostic["code"], diagnostic["operation"])

def attempt(request: dict[str, Any]) -> str:
    secure_receipts()
    path = RECEIPTS / ("nonce-" + request["nonce"] + ".json")
    # One signed request produces at most one attempt even after a crash or a
    # service restart. An unsuccessful attempt requires a NEW reviewed request.
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        return "ALREADY_CONSUMED"
    with os.fdopen(fd, "w") as out:
        json.dump({"status": "STARTED", "id": request["id"],
                   "sha": request["sha"]}, out)
        out.write("\n")
        out.flush()
        os.fsync(out.fileno())
    # Persist the nonce consumption before executing a privileged rollout.
    # A power loss must never make the same signed request replayable.
    directory_fd = os.open(RECEIPTS, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)

    status = "BLOCKED"
    executor_status = None
    diagnostic = None
    try:
        if EXECUTOR.is_symlink() or not EXECUTOR.is_file():
            raise RequestBlocked("fixed root-owned production executor missing")
        stat = EXECUTOR.stat()
        if stat.st_uid != 0 or stat.st_mode & 0o022:
            raise RequestBlocked("fixed production executor permissions unsafe")
        done = subprocess.run(
            [str(EXECUTOR), request["sha"], request["policy"],
             request["migration_policy"]],
            capture_output=True, text=True, timeout=7200, check=False,
        )
        # An executor's exit code cannot independently certify deployment.
        executor_status = executor_attestation(
            done.returncode, done.stdout, request["sha"],
        )
        status = "SUCCEEDED" if executor_status else "FAILED_REVIEW_REQUIRED"
        if not executor_status:
            diagnostic = executor_rejection(done.returncode, done.stdout, request["sha"])
    except subprocess.TimeoutExpired:
        status = "FAILED_REVIEW_REQUIRED"
        diagnostic = failure_diagnostic("EXECUTOR_TIMEOUT")
    except (OSError, RequestBlocked):
        status = "FAILED_REVIEW_REQUIRED"
        diagnostic = failure_diagnostic("EXECUTOR_UNAVAILABLE")
    # Keep a sanitized and protected audit record; never print env or secrets.
    temp = RECEIPTS / ("nonce-" + request["nonce"] + ".tmp")
    with temp.open("x", encoding="utf-8") as out:
        temp.chmod(0o600)
        json.dump({"status": status, "id": request["id"], "sha": request["sha"],
                   "executor_status": executor_status, "diagnostic": diagnostic}, out)
        out.write("\n")
        out.flush()
        os.fsync(out.fileno())
    os.replace(temp, path)
    directory_fd = os.open(RECEIPTS, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return status


def poll(now: int | None = None) -> dict[str, Any]:
    if os.geteuid() != 0 or os.uname().nodename != HOST:
        raise RequestBlocked("not the authorized OCI production VM")
    # Disabled means no GitHub request, no Docker, no changes at all.
    try:
        flag = private_root_file(GATE).decode("ascii")
    except RequestBlocked:
        return {"status": "DISABLED"}
    if flag != "KATCHA_PRODUCTION_DEPLOY_ENABLED=true":
        return {"status": "DISABLED"}
    key = key_from_file()
    current = int(time.time()) if now is None else now
    for entry in fetch_requests():
        if not isinstance(entry, dict):
            continue
        try:
            verified = authorized_request(entry, key, current)
        except RequestBlocked:
            continue
        secure_receipts()
        if (RECEIPTS / ("nonce-" + verified["nonce"] + ".json")).exists():
            continue
        result = attempt(verified)
        return {"status": result, "deployment_request_id": verified["id"],
                "sha": verified["sha"]}
    return {"status": "NO_VALID_PENDING_REQUEST"}


def main() -> int:
    try:
        result = poll()
    except (RequestBlocked, OSError, ValueError) as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc)}))
        return 2
    print(json.dumps(result, sort_keys=True))
    # Failed/blocked deployments must be visible as a failed oneshot job.
    return 2 if result["status"] in {"FAILED_REVIEW_REQUIRED"} else 0


if __name__ == "__main__":
    sys.exit(main())
