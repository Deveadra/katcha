#!/usr/bin/env python3
"""Root-owned OCI production promotion. Never invoke from an untrusted checkout.

This bounded full-stack restart intentionally preserves the existing data mount,
release history, R2, coordinator identity and external-compute safety gates.
An active Temporal workflow requires explicit approval because activities may retry.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO = Path("/opt/katcha")
ENV = Path("/etc/katcha/katcha.env")
GATE = Path("/etc/katcha/production-deploy.enabled")
STATE = Path("/srv/katcha/deployments")
LOCK = Path("/run/lock/katcha-production-deployment.lock")
SUPERVISOR_LEASE_DIR = Path("/run/katcha-production-supervisor")
SUPERVISOR_LEASE_FILE = SUPERVISOR_LEASE_DIR / "compose.lock"
API = "https://api.github.com/repos/Deveadra/katcha"
SHA = re.compile(r"[0-9a-f]{40}\Z")
EXPECTED = {
    "api", "worker", "intelligence-worker", "production-worker",
    "telegram-worker", "renderer", "postgres", "temporal", "cloudflared",
}
IMAGES = ("katcha-control", "katcha-ingest", "katcha-renderer", "katcha-production")
MIGRATION_FILE = "migrations/versions/0064_application_ai_settings.py"
MIGRATION_BLOB = "3d411641d625c772856142d6a8ce43447be7f2b5"
BEFORE_REVISION = "0063_authoritative_search_ledger"
AFTER_REVISION = "0064_application_ai_settings"
IMAGE_FOR_SERVICE = {
    "api": "katcha-control", "intelligence-worker": "katcha-control",
    "telegram-worker": "katcha-control", "worker": "katcha-ingest",
    "renderer": "katcha-renderer", "production-worker": "katcha-production",
}


class Blocked(RuntimeError):
    """Reject an unsafe deployment without exposing host credentials."""


def run(*args: str, timeout: int = 30) -> str:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Blocked("host operation failed or timed out") from exc
    if proc.returncode != 0:
        raise Blocked("host operation did not succeed: " + args[0])
    if len(proc.stdout) > 1_000_000:
        raise Blocked("host operation output unexpectedly large")
    return proc.stdout.strip()


def github(path: str) -> Any:
    if not path.startswith("/") or ".." in path or "//" in path:
        raise Blocked("invalid GitHub path")
    req = urllib.request.Request(
        API + path,
        headers={"User-Agent": "Katcha-Production-Deployment",
                 "Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            return json.load(response)
    except (urllib.error.URLError, ValueError, OSError) as exc:
        raise Blocked("GitHub evidence unavailable") from exc


def pinned(path: Path) -> tuple[str, bool]:
    if path.is_symlink() or not path.is_file():
        raise Blocked("production env missing or unsafe")
    entries: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#"):
            continue
        key, sep, val = line.partition("=")
        if sep and key in {"KATCHA_RELEASE_SHA", "KATCHA_API_RELEASE_SHA"}:
            if key in entries:
                raise Blocked("duplicate release setting")
            entries[key] = val.strip().strip("'\"")
    global_sha = entries.get("KATCHA_RELEASE_SHA", "")
    if not SHA.fullmatch(global_sha):
        raise Blocked("current release pin is not an immutable SHA")
    api_pin = entries.get("KATCHA_API_RELEASE_SHA", "")
    if api_pin and api_pin != global_sha:
        raise Blocked("mixed API pin requires separate rollout reconciliation")
    return global_sha, bool(api_pin)


def authorized() -> None:
    if os.geteuid() != 0 or run("hostname") != "katcha-prod-primary-001":
        raise Blocked("not the verified root-owned OCI production host")
    if GATE.is_symlink() or not GATE.is_file() or GATE.stat().st_uid != 0:
        raise Blocked("production deployment is not enabled locally")
    if GATE.stat().st_mode & 0o077:
        raise Blocked("local deployment gate permissions are unsafe")
    if GATE.read_text(encoding="utf-8").strip() != "KATCHA_PRODUCTION_DEPLOY_ENABLED=true":
        raise Blocked("production deployment authorization disabled")
    if run("systemctl", "is-active", "katcha.service") != "active":
        raise Blocked("production supervisor is not active")
    run("mountpoint", "-q", "/srv/katcha")


def eligible(old: str, target: str, migration_policy: str = "refuse") -> bool:
    # Deliberately re-check from the OCI host, independently of GitHub Actions.
    head = github("/branches/main")
    if head.get("commit", {}).get("sha") != target:
        raise Blocked("candidate is not current accepted main")
    # Invoke the *same* fail-closed, six-image, exact-head publication
    # verifier as the CI release gate. Installed from accepted main by
    # the root-only deployer installer, not from the mutable app checkout.
    evidence_path = "/usr/local/lib/katcha-production-deploy/release_eligibility.py"
    evidence = json.loads(run("python3", evidence_path, target, timeout=90))
    if (not isinstance(evidence, dict)
        or evidence.get("status") != "ELIGIBLE_FOR_PREDEPLOY_REVIEW"
        or evidence.get("sha") != target
        or type(evidence.get("ci_run_id")) is not int
        or type(evidence.get("image_run_id")) is not int):
        raise Blocked("independent full-image release provenance rejected")
    comparison = github("/compare/" + old + "..." + target)
    files = comparison.get("files")
    if (comparison.get("status") != "ahead"
        or comparison.get("behind_by") != 0
        or comparison.get("merge_base_commit", {}).get("sha") != old
        or not isinstance(files, list) or not files or len(files) >= 300
        or not 0 < comparison.get("total_commits", 0) <= 250):
        raise Blocked("candidate ancestry or comparison is unverified")
    migration_paths: list[str] = []
    for change in files:
        name = change.get("filename", "")
        if not isinstance(name, str) or not name or ".." in name.split("/"):
            raise Blocked("candidate changed-file list invalid")
        if name == "deploy/scripts/postgres-disaster-restore.sh":
            raise Blocked("disaster-restore code changed; separate review required")
        if name.startswith(("alembic/", "migrations/")) or name == "alembic.ini":
            if change.get("status") != "added" and name == MIGRATION_FILE:
                raise Blocked("approved migration must be an added immutable file")
            migration_paths.append(name)
    if migration_policy not in {"refuse", "approve-additive-0064"}:
        raise Blocked("unrecognized migration approval")
    if migration_paths:
        if migration_policy != "approve-additive-0064" or migration_paths != [MIGRATION_FILE]:
            raise Blocked("unknown migration or migration approval missing")
        proof = github("/contents/" + MIGRATION_FILE + "?ref=" + target)
        if (not isinstance(proof, dict) or proof.get("sha") != MIGRATION_BLOB
            or proof.get("path") != MIGRATION_FILE
            or proof.get("type") != "file"):
            raise Blocked("approved additive migration source differs from reviewed blob")
    elif migration_policy != "refuse":
        raise Blocked("migration approval supplied for release without reviewed migration")
    if github("/branches/main").get("commit", {}).get("sha") != target:
        raise Blocked("main advanced during deployment review")
    return bool(migration_paths)


def runtime(sha: str, staged: dict[str, str] | None = None) -> None:
    ids = run("docker", "ps", "--filter", "label=com.docker.compose.project=katcha-production",
              "--format", "{{.ID}}").splitlines()
    if len(ids) != 9 or len(set(ids)) != 9:
        raise Blocked("unexpected production service count")
    services = set()
    for cid in ids:
        row = json.loads(run("docker", "inspect", cid))[0]
        service = row["Config"]["Labels"].get("com.docker.compose.service")
        state = row["State"]
        if service not in EXPECTED or service in services:
            raise Blocked("unrecognized or duplicate service")
        if not state.get("Running") or state.get("Status") != "running":
            raise Blocked("production container not running")
        if service in {"api", "postgres", "temporal", "renderer"}:
            if state.get("Health", {}).get("Status") != "healthy":
                raise Blocked("production container unhealthy")
        if service not in {"postgres", "temporal", "cloudflared"}:
            img = row["Config"]["Image"]
            name = IMAGE_FOR_SERVICE[service]
            if img != "ghcr.io/deveadra/" + name + ":" + sha:
                raise Blocked("running application image does not match release")
            if staged is not None and row.get("Image") != staged[name]:
                raise Blocked("running image differs from previously staged immutable image")
        services.add(service)
    if services != EXPECTED:
        raise Blocked("required production service absent")
    try:
        with urllib.request.urlopen(
            "http://127.0.0.1:8000/v1/health/release", timeout=6,
        ) as response:
            release = json.load(response)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise Blocked("local API release endpoint not ready") from exc
    if release.get("release_sha") != sha:
        raise Blocked("local API release attestation mismatch")


def authority() -> None:
    # Reuse the *already running* Katcha API container's configured fence token;
    # the token never enters a shell argument or the deployment job output.
    check = (
        "from katcha.runtime_fence import assert_mutation_authority; "
        "receipt=assert_mutation_authority('production.deployment'); "
        "assert receipt is not None and receipt.active_epoch==receipt.deployment_epoch "
        "and receipt.leader_id==receipt.deployment_id"
    )
    run("docker", "exec", "katcha-production-api-1", "python", "-c", check, timeout=20)


def workflows(policy: str) -> int:
    output = run("docker", "exec", "katcha-production-temporal-1", "temporal",
                 "workflow", "count", "--address", "temporal:7233",
                 "--namespace", "default", "--query", 'ExecutionStatus="Running"')
    match = re.fullmatch(r"Total:\s*(\d+)", output)
    if not match:
        raise Blocked("unable to verify running Temporal workflows")
    count = int(match.group(1))
    if count > 100:
        raise Blocked("unexpected number of running Temporal workflows")
    if count and policy != "approved-resume":
        raise Blocked("Temporal workflows still running; explicit approval required")
    return count


def temporal_inventory(policy: str) -> tuple[tuple[str, str], ...]:
    """Inspect all open executions and forbid interrupted in-flight activities."""
    count = workflows(policy)
    if not count:
        return ()
    raw = run(
        "docker", "exec", "katcha-production-temporal-1", "temporal",
        "--output", "json", "workflow", "list",
        "--address", "temporal:7233", "--namespace", "default",
        "--query", 'ExecutionStatus="Running"', "--limit", "101",
        timeout=30,
    )
    try:
        executions = json.loads(raw)
    except ValueError as exc:
        raise Blocked("Temporal execution inventory is not machine-readable") from exc
    if not isinstance(executions, list) or len(executions) != count:
        raise Blocked("Temporal execution inventory differs from live count")
    ids: set[tuple[str, str]] = set()
    for item in executions:
        if not isinstance(item, dict) or not isinstance(item.get("execution"), dict):
            raise Blocked("Temporal workflow identity missing")
        execution = item["execution"]
        wid, rid = execution.get("workflowId"), execution.get("runId")
        if (not isinstance(wid, str) or not wid or len(wid) > 255
            or not isinstance(rid, str) or not rid or len(rid) > 128
            or "\x00" in wid or "\x00" in rid):
            raise Blocked("Temporal workflow identity malformed")
        ident = (wid, rid)
        if ident in ids:
            raise Blocked("Temporal workflow inventory contains duplicate run")
        ids.add(ident)
        try:
            details = json.loads(run(
                "docker", "exec", "katcha-production-temporal-1", "temporal",
                "--output", "json", "workflow", "describe",
                "--address", "temporal:7233", "--namespace", "default",
                "--workflow-id", wid, "--run-id", rid, timeout=30,
            ))
        except ValueError as exc:
            raise Blocked("Temporal activity state is not machine-readable") from exc
        if not isinstance(details, dict) or not isinstance(
            details.get("workflowExecutionInfo"), dict
        ):
            raise Blocked("Temporal workflow description incomplete")
        info = details["workflowExecutionInfo"]
        observed = info.get("execution")
        if (not isinstance(observed, dict)
            or observed.get("workflowId") != wid or observed.get("runId") != rid):
            raise Blocked("Temporal workflow description changed during inspection")
        for field in ("pendingActivities", "pendingChildren",
                      "pendingWorkflowTask", "pendingNexusOperations"):
            pending = details.get(field, [])
            if pending not in (None, False, [], {}):
                raise Blocked("Temporal has pending activities or tasks; drain required")
    return tuple(sorted(ids))


def verify_temporal_recovery(
    original: tuple[tuple[str, str], ...],
) -> dict[str, int]:
    """Find every pre-stop run in durable Temporal state after restart."""
    statuses: dict[str, int] = {"running": 0, "completed": 0, "continued_as_new": 0}
    for wid, rid in original:
        try:
            details = json.loads(run(
                "docker", "exec", "katcha-production-temporal-1", "temporal",
                "--output", "json", "workflow", "describe",
                "--address", "temporal:7233", "--namespace", "default",
                "--workflow-id", wid, "--run-id", rid, timeout=30,
            ))
        except ValueError as exc:
            raise Blocked("previous Temporal execution could not be described") from exc
        if not isinstance(details, dict) or not isinstance(
            details.get("workflowExecutionInfo"), dict
        ):
            raise Blocked("previous Temporal execution history not observed")
        info = details["workflowExecutionInfo"]
        ident = info.get("execution")
        if not isinstance(ident, dict) or (
            ident.get("workflowId"), ident.get("runId")
        ) != (wid, rid):
            raise Blocked("previous Temporal workflow identity differs after restart")
        status = info.get("status")
        normalized = re.sub(r"[^a-z]", "", status.lower()) if isinstance(status, str) else ""
        if normalized.endswith("continuedasnew"):
            statuses["continued_as_new"] += 1
        elif normalized.endswith("completed"):
            statuses["completed"] += 1
        elif normalized.endswith("running"):
            statuses["running"] += 1
        else:
            raise Blocked("previous Temporal execution failed or has unknown status")
    return statuses


def database_query(sql: str) -> str:
    """Static PostgreSQL observations in the already-running database container."""
    statements = {
        "revision": "SELECT version_num FROM alembic_version",
        "table": "SELECT COALESCE(to_regclass('public.application_ai_settings')::text, '')",
        "columns": (
            "SELECT column_name || ':' || data_type || ':' || is_nullable || ':' "
            "|| COALESCE(character_maximum_length::text, '') "
            "FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'application_ai_settings' "
            "ORDER BY ordinal_position"
        ),
        "primary": (
            "SELECT string_agg(kcu.column_name, ',' ORDER BY kcu.ordinal_position) "
            "FROM information_schema.table_constraints tc "
            "JOIN information_schema.key_column_usage kcu "
            "ON tc.constraint_name = kcu.constraint_name "
            "AND tc.table_schema = kcu.table_schema "
            "WHERE tc.table_schema = 'public' AND tc.table_name = 'application_ai_settings' "
            "AND tc.constraint_type = 'PRIMARY KEY'"
        ),
    }
    if sql not in statements:
        raise Blocked("unapproved database observation")
    query = statements[sql].replace("'", "'\\''")
    cmd = (
        'psql -X -qAt -v ON_ERROR_STOP=1 '
        '-U "${POSTGRES_USER:?}" -d "${POSTGRES_DB:?}" '
        "-c '" + query + "'"
    )
    return run("docker", "exec", "katcha-production-postgres-1",
               "sh", "-ec", cmd, timeout=20)


def database_migration_preflight() -> None:
    if database_query("revision") != BEFORE_REVISION:
        raise Blocked("database revision differs from reviewed additive migration base")
    if database_query("table"):
        raise Blocked("application AI settings table already exists before migration")


def database_migration_postflight() -> None:
    if database_query("revision") != AFTER_REVISION:
        raise Blocked("database was not upgraded to the reviewed target revision")
    if database_query("table") != "application_ai_settings":
        raise Blocked("approved AI settings table missing after migration")
    if database_query("columns").splitlines() != [
        "key:character varying:NO:64", "value:text:NO:",
    ] or database_query("primary") != "key":
        raise Blocked("approved AI settings table schema differs from reviewed migration")


def stage_images(target: str) -> dict[str, str]:
    staged: dict[str, str] = {}
    for name in IMAGES:
        ref = "ghcr.io/deveadra/" + name + ":" + target
        run("docker", "pull", ref, timeout=900)
        output = run("docker", "image", "inspect", ref,
                     "--format", '{{.Os}} {{.Architecture}} {{index .Config.Labels "org.opencontainers.image.revision"}}')
        if output != "linux arm64 " + target:
            raise Blocked("image platform or source revision mismatch")
        digest = run("docker", "image", "inspect", ref,
                     "--format", "{{json .RepoDigests}}")
        values = json.loads(digest)
        if not isinstance(values, list) or not any(
            x.startswith("ghcr.io/deveadra/" + name + "@sha256:")
            for x in values if isinstance(x, str)
        ):
            raise Blocked("pinned published image digest evidence missing")
        image_id = run("docker", "image", "inspect", ref, "--format", "{{.Id}}")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
            raise Blocked("staged image identity malformed")
        staged[name] = image_id
    return staged


def backup_and_restore() -> None:
    markers = {
        "katcha-backup.service": "Katcha PostgreSQL backup completed:",
        "katcha-restore-test.service": "Katcha disaster restore test passed for 3 databases.",
    }
    for name, marker in markers.items():
        if run("systemctl", "show", "-p", "ActiveState", "--value", name) != "inactive":
            raise Blocked("database safety job is not idle")
        started = "@" + str(int(time.time()))
        run("systemctl", "start", name, timeout=6000)
        if run("systemctl", "show", "-p", "Result", "--value", name) != "success":
            raise Blocked("fresh backup or restore verification failed")
        if run("systemctl", "show", "-p", "ExecMainStatus", "--value", name) != "0":
            raise Blocked("fresh backup or restore verification exited nonzero")
        # A oneshot script can return success when a lock is already held and
        # work was skipped. Require positive *new* completion evidence.
        log = run("journalctl", "-u", name, "--since", started,
                  "--no-pager", "-o", "cat", timeout=30)
        if marker not in log:
            raise Blocked("fresh backup or three-database restore proof missing")


def atomic_env(content: bytes) -> None:
    """Replace the root-owned secret file without a truncation window."""
    stat = ENV.stat()
    fd, tmp_name = tempfile.mkstemp(prefix=".katcha.env.", dir=ENV.parent)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(content)
            out.flush()
            os.fsync(out.fileno())
        os.chown(tmp_name, stat.st_uid, stat.st_gid)
        os.chmod(tmp_name, stat.st_mode & 0o777)
        os.replace(tmp_name, ENV)
        directory_fd = os.open(ENV.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def update_pin(target: str, backup: Path) -> None:
    original = ENV.read_bytes()
    backup.write_bytes(original)
    backup.chmod(0o600)
    rows = original.decode("utf-8").splitlines(keepends=True)
    old_sha, _ = pinned(ENV)
    count = 0
    api_count = 0
    for i, row in enumerate(rows):
        if row.startswith("KATCHA_RELEASE_SHA="):
            rows[i] = "KATCHA_RELEASE_SHA=" + target + "\n"
            count += 1
        elif row.startswith("KATCHA_API_RELEASE_SHA="):
            existing = row.partition("=")[2].strip().strip("'\\\"")
            if existing and existing != old_sha:
                raise Blocked("API pin does not match predeployment global release")
            rows[i] = ("KATCHA_API_RELEASE_SHA=" + target + "\n"
                       if existing else "KATCHA_API_RELEASE_SHA=\n")
            api_count += 1
    if count != 1 or api_count > 1:
        raise Blocked("invalid release pin count")
    atomic_env("".join(rows).encode("utf-8"))

def healthy(target: str, seconds: int = 300,
            staged: dict[str, str] | None = None) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if run("systemctl", "is-active", "katcha.service") == "active":
                runtime(target, staged=staged)
                authority()
                return
        except Blocked:
            pass
        time.sleep(5)
    raise Blocked("deployed services failed bounded readiness/authority checks")



def acquire_supervisor_lease() -> int:
    """Serialize checkout/pin mutation with the active Compose owner's lease.

    The existing pre-lease supervisor must first be stopped and its nine
    containers confirmed stopped. Release before starting its successor.
    """
    path = SUPERVISOR_LEASE_DIR
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise Blocked("unsafe shared production Compose lease directory")
    path.mkdir(mode=0o700, exist_ok=True)
    stat = path.stat()
    if stat.st_uid != 0 or stat.st_mode & 0o777 != 0o700:
        raise Blocked("shared production Compose lease directory not root-private")
    if SUPERVISOR_LEASE_FILE.is_symlink():
        raise Blocked("unsafe shared production Compose lease path")
    fd = os.open(SUPERVISOR_LEASE_FILE, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        stat = os.fstat(fd)
        if stat.st_uid != 0 or stat.st_mode & 0o777 != 0o600:
            raise Blocked("shared production Compose lease file not root-private")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, Blocked) as exc:
        os.close(fd)
        raise Blocked("production Compose ownership lease unavailable") from exc
    return fd

def deploy(target: str, policy: str, migration_policy: str = "refuse") -> dict[str, Any]:
    if (not SHA.fullmatch(target) or policy not in {"refuse", "approved-resume"}
        or migration_policy not in {"refuse", "approve-additive-0064"}):
        raise Blocked("invalid deployment inputs")
    authorized()
    current, _ = pinned(ENV)
    if target == current:
        runtime(current)
        return {"status": "ALREADY_CURRENT", "sha": current}
    if run("git", "-C", str(REPO), "status", "--porcelain"):
        raise Blocked("production checkout has uncommitted changes")
    if run("git", "-C", str(REPO), "branch", "--show-current") != "main":
        raise Blocked("production checkout not on main")
    if run("git", "-C", str(REPO), "rev-parse", "HEAD") != current:
        raise Blocked("production checkout diverges from pinned release")
    if run("git", "-C", str(REPO), "remote", "get-url", "origin") not in {
        "https://github.com/Deveadra/katcha.git", "git@github.com:Deveadra/katcha.git",
    }:
        raise Blocked("unrecognized production origin")
    runtime(current)
    authority()
    migration = eligible(current, target, migration_policy)
    if migration:
        database_migration_preflight()
    before = temporal_inventory(policy)
    staged = stage_images(target)
    backup_and_restore()
    # All operations above are preparation-only. Recheck authority, workflow
    # policy and verified release immediately before the maintenance window.
    authority()
    if temporal_inventory(policy) != before:
        raise Blocked("running Temporal executions changed during backup/preparation")
    if eligible(current, target, migration_policy) != migration:
        raise Blocked("migration classification changed during deployment review")
    if migration:
        database_migration_preflight()
    run("git", "-C", str(REPO), "fetch", "origin", "main", timeout=60)
    if run("git", "-C", str(REPO), "rev-parse", "origin/main") != target:
        raise Blocked("fetched main differs from approved candidate")
    run("git", "-C", str(REPO), "merge-base", "--is-ancestor", current, target)
    if migration and run(
        "git", "-C", str(REPO), "rev-parse", target + ":" + MIGRATION_FILE,
    ) != MIGRATION_BLOB:
        raise Blocked("fetched migration blob differs from approved implementation")
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    if STATE.is_symlink() or STATE.stat().st_uid != 0 or STATE.stat().st_mode & 0o077:
        raise Blocked("deployment audit directory ownership/permissions unsafe")
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    record_dir = STATE / (stamp + "-" + target[:12])
    record_dir.mkdir(mode=0o700)
    backup = record_dir / "production-env.before"
    original = ENV.read_bytes()
    backup.write_bytes(original)
    backup.chmod(0o600)
    started_change = False
    try:
        # Start recovery obligation BEFORE requesting the stop: systemctl can
        # time out after already stopping some or all of the running stack.
        started_change = True
        run("systemctl", "stop", "katcha.service", timeout=180)
        remaining = run("docker", "ps", "--filter",
                        "label=com.docker.compose.project=katcha-production",
                        "--format", "{{.ID}}")
        if remaining:
            raise Blocked("old Compose supervisor did not fully stop")
        if run("systemctl", "show", "-p", "ActiveState", "--value",
               "katcha.service") != "inactive":
            raise Blocked("old Compose supervisor is still active")
        lease_fd = acquire_supervisor_lease()
        try:
            run("git", "-C", str(REPO), "merge", "--ff-only", target, timeout=60)
            update_pin(target, backup)
        finally:
            os.close(lease_fd)
        # The successor must obtain the same private lease itself.
        run("systemctl", "start", "katcha.service", timeout=900)
        healthy(target, staged=staged)
        if migration:
            database_migration_postflight()
        resumed = workflows("approved-resume")
        recovered = verify_temporal_recovery(before)
        outcome = {"status": "DEPLOYED", "sha": target, "previous_sha": current,
                   "predeploy_running_workflows": len(before),
                   "postdeploy_running_workflows": resumed,
                   "predeploy_workflow_recovery": recovered,
                   "migration_revision": AFTER_REVISION if migration else "unchanged",
                   "migration_rollback": "FORWARD_ONLY" if migration else "not_applicable",
                   "public_browser_acceptance": "REQUIRED"}
    except Exception as exc:
        # An upgraded database contains a revision unknown to the previous
        # image. Never blindly restart an old image after a migration-bearing
        # change; preserve data and require a supervised forward repair.
        if migration and started_change:
            outcome = {
                "status": "RECOVERY_REQUIRED", "sha": target,
                "previous_sha": current, "migration": AFTER_REVISION,
                "reason": "forward-only migration release needs supervised repair",
            }
            (record_dir / "result.json").write_text(json.dumps(outcome) + "\n")
            raise Blocked("forward-only migration rollout failed; old image rollback prohibited") from exc
        if started_change:
            try:
                run("systemctl", "stop", "katcha.service", timeout=180)
                if run("systemctl", "show", "-p", "ActiveState", "--value",
                       "katcha.service") != "inactive":
                    raise Blocked("supervisor still active during rollback")
                remaining = run(
                    "docker", "ps", "--filter",
                    "label=com.docker.compose.project=katcha-production",
                    "--format", "{{.ID}}",
                )
                if remaining:
                    raise Blocked("old containers still running during rollback")
                lease_fd = acquire_supervisor_lease()
                try:
                    atomic_env(original)
                    if run("git", "-C", str(REPO), "rev-parse", "HEAD") != current:
                        run("git", "-C", str(REPO), "switch", "--detach", current)
                finally:
                    os.close(lease_fd)
                run("systemctl", "start", "katcha.service", timeout=900)
                healthy(current)
                outcome = {"status": "ROLLED_BACK", "sha": current,
                           "reason": "new release readiness failed",
                           "checkout_state": "detached; operator reconciliation required"}
            except Exception:
                outcome = {"status": "RECOVERY_REQUIRED", "sha": current,
                           "reason": "automatic non-destructive rollback failed"}
            (record_dir / "result.json").write_text(json.dumps(outcome) + "\n")
        raise Blocked("deployment unsuccessful; inspect protected on-host audit and service health") from exc
    (record_dir / "result.json").write_text(json.dumps(outcome) + "\n")
    return outcome


def readiness() -> dict[str, Any]:
    """Read-only host-side rehearsal; does not require or create a deploy gate."""
    if os.geteuid() != 0 or run("hostname") != "katcha-prod-primary-001":
        raise Blocked("not the verified OCI production primary")
    run("mountpoint", "-q", "/srv/katcha")
    old, _ = pinned(ENV)
    runtime(old)
    database_migration_preflight()
    open_executions = temporal_inventory("approved-resume")
    return {
        "status": "REVIEW_REQUIRED",
        "migration": AFTER_REVISION,
        "database_start_revision": BEFORE_REVISION,
        "temporal_running_count": len(open_executions),
        "temporal_pending_activity_check": "PASSED",
        "note": "Read-only preflight. Not a deployment authorization.",
    }


def main() -> int:
    if sys.argv[1:] == ["--readiness"]:
        try:
            print(json.dumps(readiness(), sort_keys=True))
            return 0
        except (Blocked, OSError, ValueError, KeyError) as exc:
            print(json.dumps({"status": "BLOCKED", "reason": str(exc)}))
            return 2
    if len(sys.argv) != 4:
        print("BLOCKED: expected exact SHA, workflow and migration policies", file=sys.stderr)
        return 2
    try:
        fd = os.open(LOCK, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise Blocked("another production deployment is active") from exc
            result = deploy(sys.argv[1], sys.argv[2], sys.argv[3])
    except (Blocked, OSError, ValueError, KeyError) as exc:
        print("DEPLOY_BLOCKED: " + str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
