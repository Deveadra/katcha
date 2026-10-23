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
import stat
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
SIGNING_KEY = Path("/etc/katcha/production-deploy.key")
TOOL_SOURCE = Path("/usr/local/lib/katcha-production-deploy/installed-source-sha")
STATE = Path("/srv/katcha/deployments")
LOCK = Path("/run/lock/katcha-production-deployment.lock")
SUPERVISOR_LEASE_DIR = Path("/run/katcha-production-supervisor")
SUPERVISOR_LEASE_FILE = SUPERVISOR_LEASE_DIR / "compose.lock"
API = "https://api.github.com/repos/Deveadra/katcha"
SHA = re.compile(r"[0-9a-f]{40}\Z")
EXPECTED = {
    "api", "worker", "intelligence-worker", "production-worker", "browser",
    "telegram-worker", "renderer", "postgres", "temporal", "cloudflared",
}
IMAGES = (
    "katcha-browser", "katcha-control", "katcha-ingest", "katcha-renderer", "katcha-production",
)
MIGRATION_FILE = "migrations/versions/0064_application_ai_settings.py"
MIGRATION_BLOB = "3d411641d625c772856142d6a8ce43447be7f2b5"
BEFORE_REVISION = "0063_authoritative_search_ledger"
AFTER_REVISION = "0064_application_ai_settings"
MIGRATION_0065_FILE = "migrations/versions/0065_acquisition_jobs.py"
MIGRATION_0065_BLOB = "1a7debea0c609bd3a6a5f19c3b310b64077e9013"
LATEST_REVISION = "0065_acquisition_jobs"
HANDOFF_REVISION = "0066_acquisition_handoff"
HANDOFF_FILE = "migrations/versions/0066_acquisition_handoff.py"
HANDOFF_BLOB = "5e5facd8557a97ab788d529f0b40b8740b414df2"
MIGRATION_POLICIES = {"refuse", "approve-additive-0064", "approve-additive-through-0065",
                      "approve-additive-through-0066"}
REVIEWED_MIGRATIONS = {
    MIGRATION_FILE: MIGRATION_BLOB,
    MIGRATION_0065_FILE: MIGRATION_0065_BLOB,
}
ACQUISITION_CONSTRAINTS = [
    "acquisition_jobs_pkey:p:PRIMARY KEY (id)",
    "ck_acquisition_job_generation:c:CHECK (generation >= 0)",
    "ck_acquisition_job_lease:c:CHECK (status::text <> 'leased'::text OR "
    'worker_principal IS NOT NULL AND claim_id IS NOT NULL AND lease_expires_at '
    'IS NOT NULL AND generation > 0)',
    "ck_acquisition_job_max_bytes:c:CHECK (max_bytes > 0)",
    "ck_acquisition_job_progress:c:CHECK (bytes_received >= 0 AND bytes_received <= max_bytes)",
    "ck_acquisition_job_sequence:c:CHECK (heartbeat_sequence >= 0)",
    'ck_acquisition_job_status:c:CHECK (status::text = ANY '
    "(ARRAY['queued'::character varying, 'leased'::character varying, "
    "'awaiting_validation'::character varying, 'failed'::character varying, "
    "'cancelled'::character varying, 'recovery_required'::character "
    'varying]::text[]))',
    'fk_acquisition_job_receipt_channel:f:FOREIGN KEY (receipt_id, '
    'channel_profile_id) REFERENCES acquisition_receipts(id, channel_profile_id)',
    "uq_acquisition_job_receipt:u:UNIQUE (receipt_id)",
]
IMAGE_FOR_SERVICE = {
    "browser": "katcha-browser",
    "api": "katcha-control", "intelligence-worker": "katcha-control",
    "telegram-worker": "katcha-control", "worker": "katcha-ingest",
    "renderer": "katcha-renderer", "production-worker": "katcha-production",
    "local-acquisition": "katcha-ingest", "analysis-worker": "katcha-analysis",
}


class Blocked(RuntimeError):
    """Reject an unsafe deployment without exposing host credentials."""


# The root-installed independent verifier produces these exact, controlled
# rejection reasons. Never surface its raw stdout/stderr: unexpected output,
# even from an installed executable, may contain sensitive infrastructure data.
SAFE_RELEASE_VERIFIER_REASONS = frozenset({
    "invalid GitHub API path",
    "GitHub release evidence unavailable",
    "missing workflow run list",
    "workflow run response malformed",
    "workflow repository response malformed",
    "workflow run identity malformed",
    "publication evidence absent",
    "too many image publication attempts for one CI",
    "publication job evidence malformed",
    "publication job evidence missing or truncated",
    "publication job identity malformed",
    "publication job incomplete or duplicated",
    "publication verification jobs missing",
    "skipped publication contains unexpected work",
    "latest image publication failed verification",
    "production image build evidence incomplete",
    "not all seven runtime images were verified",
    "no completed image publication for the current revision",
    "workflow timestamp malformed",
    "workflow timestamp must include timezone",
    "invalid commit identity",
    "release candidate is not an accepted main ancestor",
    "matching CI and image publication runs not found",
    "latest matching run not successful",
    "image publication does not follow successful CI",
    "sha must be an exact lowercase 40-character git commit",
    "invalid main branch response",
    "invalid main commit response",
    "invalid main commit identity",
    "workflow evidence response malformed",
    "workflow evidence count malformed",
    "workflow evidence truncated; refuse to infer approval",
    "latest push CI did not pass",
})


def run(*args: str, timeout: int = 30, release_verifier: bool = False) -> str:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        if release_verifier:
            raise Blocked("independent release verifier failed to start or timed out") from exc
        raise Blocked("host operation failed or timed out") from exc
    if proc.returncode != 0:
        if release_verifier and len(proc.stdout) <= 4096:
            try:
                detail = json.loads(proc.stdout)
            except (ValueError, TypeError):
                detail = None
            if (
                isinstance(detail, dict)
                and set(detail) == {"status", "reason"}
                and detail["status"] == "BLOCKED"
                and isinstance(detail["reason"], str)
                and detail["reason"] in SAFE_RELEASE_VERIFIER_REASONS
            ):
                raise Blocked("independent release verifier blocked: " + detail["reason"])
        if release_verifier:
            raise Blocked("independent release verifier rejected evidence without a safe reason")
        raise Blocked("host operation did not succeed: " + args[0])
    if len(proc.stdout) > 1_000_000:
        raise Blocked("host operation output unexpectedly large")
    return proc.stdout.strip()


def github(path: str) -> Any:
    canonical_compare = re.fullmatch(r"/compare/[0-9a-f]{40}\.\.\.[0-9a-f]{40}", path)
    if not path.startswith("/") or (".." in path and not canonical_compare) or "//" in path:
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


def accepted_main_ancestry(target: str) -> None:
    """The exact candidate must still belong to accepted main history."""
    head = github("/branches/main")
    commit = head.get("commit") if isinstance(head, dict) else None
    main_sha = commit.get("sha") if isinstance(commit, dict) else None
    if not isinstance(main_sha, str) or not SHA.fullmatch(main_sha):
        raise Blocked("accepted main identity unavailable")
    if main_sha == target:
        return
    ancestry = github("/compare/" + target + "..." + main_sha)
    merge_base = ancestry.get("merge_base_commit") if isinstance(ancestry, dict) else None
    if (not isinstance(ancestry, dict)
        or ancestry.get("status") != "ahead"
        or ancestry.get("behind_by") != 0
        or not isinstance(merge_base, dict)
        or merge_base.get("sha") != target):
        raise Blocked("candidate is no longer an accepted main ancestor")


def eligible(old: str, target: str, migration_policy: str = "refuse") -> bool:
    # Deliberately re-check from OCI, independently of GitHub Actions.
    accepted_main_ancestry(target)
    # Invoke the fail-closed seven-image, exact-candidate publication
    # verifier as the CI release gate. Installed from accepted main by
    # the root-only deployer installer, not from the mutable app checkout.
    evidence_path = "/usr/local/lib/katcha-production-deploy/release_eligibility.py"
    evidence = json.loads(run("python3", evidence_path, target, timeout=90,
                              release_verifier=True))
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
        or not isinstance(comparison.get("merge_base_commit"), dict)
        or comparison["merge_base_commit"].get("sha") != old
        or not isinstance(files, list) or not files or len(files) >= 300
        or not 0 < comparison.get("total_commits", 0) <= 250):
        raise Blocked("candidate ancestry or comparison is unverified")
    migration_paths: list[str] = []
    for change in files:
        name = change.get("filename", "")
        if not isinstance(name, str) or not name or ".." in name.split("/"):
            raise Blocked("candidate changed-file list invalid")
        previous = change.get("previous_filename", "")
        if previous and (not isinstance(previous, str)
                         or previous.startswith(("alembic/", "migrations/"))
                         or previous == "alembic.ini"):
            raise Blocked("migration rename is outside reviewed additive boundary")
        if name == "deploy/scripts/postgres-disaster-restore.sh":
            raise Blocked("disaster-restore code changed; separate review required")
        if name.startswith(("alembic/", "migrations/")) or name == "alembic.ini":
            if change.get("status") != "added" and name == MIGRATION_FILE:
                raise Blocked("approved migration must be an added immutable file")
            migration_paths.append(name)
    if migration_policy not in MIGRATION_POLICIES:
        raise Blocked("unrecognized migration approval")
    if migration_paths:
        allowed = ({MIGRATION_FILE} if migration_policy == "approve-additive-0064"
                   else {HANDOFF_FILE}
                   if migration_policy == "approve-additive-through-0066"
                   else set(REVIEWED_MIGRATIONS)
                   if migration_policy == "approve-additive-through-0065" else set())
        if (not set(migration_paths) <= allowed
            or len(set(migration_paths)) != len(migration_paths)
            or (migration_policy == "approve-additive-through-0065"
                and MIGRATION_0065_FILE not in migration_paths)):
            raise Blocked("unknown migration or migration approval missing")
        for change in files:
            if change.get("filename") in migration_paths and change.get("status") != "added":
                raise Blocked("approved migration must be an added immutable file")
    elif migration_policy != "refuse":
        raise Blocked("migration approval supplied for release without reviewed migration")
    # Even a migration-free release must retain the reviewed head. Inspect the
    # target inventory rather than accepting the revision of the running image.
    reviewed = ({MIGRATION_FILE: MIGRATION_BLOB}
                if migration_policy == "approve-additive-0064" else REVIEWED_MIGRATIONS)
    max_revision = 64 if migration_policy == "approve-additive-0064" else 65
    inventory = github("/contents/migrations/versions?ref=" + target)
    if (not isinstance(inventory, list) or not inventory or len(inventory) >= 1000
        or any(not isinstance(item, dict) or not isinstance(item.get("name"), str)
               for item in inventory)):
        raise Blocked("target migration inventory unavailable")
    by_name = {item["name"]: item for item in inventory}
    if migration_policy == "approve-additive-through-0066" or (
        migration_policy == "refuse" and Path(HANDOFF_FILE).name in by_name
    ):
        reviewed = {**REVIEWED_MIGRATIONS, HANDOFF_FILE: HANDOFF_BLOB}
        max_revision = 66
    if (len(by_name) != len(inventory)
        or any(name.endswith(".py") and name[:4].isdigit()
               and int(name[:4]) > max_revision for name in by_name)
        or any(by_name.get(Path(path).name, {}).get("type") != "file"
               or by_name[Path(path).name].get("sha") != blob
               for path, blob in reviewed.items())):
        raise Blocked("target migration head is outside reviewed boundary")
    for path, blob in reviewed.items():
        proof = github("/contents/" + path + "?ref=" + target)
        if (not isinstance(proof, dict) or proof.get("sha") != blob
            or proof.get("path") != path or proof.get("type") != "file"):
            raise Blocked("approved additive migration source differs from reviewed blob")
    accepted_main_ancestry(target)
    return bool(migration_paths)


def single_host_profile(compose_source: str) -> bool:
    # Old immutable releases do not consume this opt-in. This also preserves
    # rollback to the previous default stack during first activation.
    if "single-host-profile-v1" not in compose_source:
        return False
    values = []
    for line in ENV.read_text().splitlines():
        key, separator, value = line.strip().partition("=")
        if key.strip() == "KATCHA_SINGLE_HOST_PROFILE_ENABLED":
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            if not separator or value not in {"true", "false"}:
                raise Blocked("invalid single-host profile selection")
            values.append(value)
    if len(values) > 1:
        raise Blocked("duplicate single-host profile selection")
    return values == ["true"]


def runtime(sha: str, staged: dict[str, str] | None = None) -> None:
    # The previous immutable release may predate the browser service. Derive its
    # required inventory from that exact revision so first rollout and rollback
    # preserve the existing strict service-count checks.
    compose_source = run("git", "-C", str(REPO), "show",
                         sha + ":deploy/docker-compose.production.yml")
    expected = EXPECTED if "\n  browser:\n" in compose_source else EXPECTED - {"browser"}
    if single_host_profile(compose_source):
        expected = expected | {"local-acquisition", "analysis-worker"}
    ids = run("docker", "ps", "--filter", "label=com.docker.compose.project=katcha-production",
              "--format", "{{.ID}}").splitlines()
    if len(ids) != len(expected) or len(set(ids)) != len(expected):
        raise Blocked("unexpected production service count")
    services = set()
    for cid in ids:
        row = json.loads(run("docker", "inspect", cid))[0]
        service = row["Config"]["Labels"].get("com.docker.compose.service")
        state = row["State"]
        if service not in expected or service in services:
            raise Blocked("unrecognized or duplicate service")
        if not state.get("Running") or state.get("Status") != "running":
            raise Blocked("production container not running")
        if (service in {"api", "postgres", "temporal", "renderer", "browser"}
            and state.get("Health", {}).get("Status") != "healthy"):
            raise Blocked("production container unhealthy")
        if service not in {"postgres", "temporal", "cloudflared"}:
            img = row["Config"]["Image"]
            name = IMAGE_FOR_SERVICE[service]
            if img != "ghcr.io/deveadra/" + name + ":" + sha:
                raise Blocked("running application image does not match release")
            if staged is not None and row.get("Image") != staged[name]:
                raise Blocked("running image differs from previously staged immutable image")
        services.add(service)
    if services != expected:
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


def temporal_inventory(policy: str, *, schedulers_only: bool = False) -> tuple[tuple[str, str], ...]:
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
        if schedulers_only and (not isinstance(item.get("type"), dict)
                                or item["type"].get("name") not in {
            "AutomaticResearchWorkflow", "TopicWatchScheduleWorkflow",
            "ChannelIntelligenceScheduleWorkflow",
        }):
            raise Blocked("first single-host activation requires media workflows to drain")
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
    for prefix, table in (("settings", "application_ai_settings"),
                          ("acquisition", "acquisition_jobs"),
                          ("transfer", "acquisition_transfers")):
        statements[prefix + "_table"] = (
            "SELECT COALESCE(to_regclass('public." + table + "')::text, '')"
        )
        statements[prefix + "_columns"] = (
            "SELECT column_name || ':' || data_type || ':' || is_nullable || ':' "
            "|| COALESCE(character_maximum_length::text, '') "
            "FROM information_schema.columns WHERE table_schema = 'public' "
            "AND table_name = '" + table + "' ORDER BY ordinal_position"
        )
        statements[prefix + "_constraints"] = (
            "SELECT conname || ':' || contype::text || ':' || pg_get_constraintdef(oid, true) "
            "|| CASE WHEN convalidated THEN '' ELSE ':NOT_VALIDATED' END "
            "FROM pg_constraint WHERE conrelid = to_regclass('public." + table + "') "
            "ORDER BY conname"
        )
        statements[prefix + "_indexes"] = (
            "SELECT p.indexname || ':' || p.indexdef "
            "|| CASE WHEN i.indisvalid AND i.indisready AND i.indislive "
            "THEN '' ELSE ':INVALID' END FROM pg_indexes p "
            "JOIN pg_namespace n ON n.nspname = p.schemaname "
            "JOIN pg_class c ON c.relnamespace = n.oid AND c.relname = p.indexname "
            "JOIN pg_index i ON i.indexrelid = c.oid "
            "WHERE p.schemaname = 'public' AND p.tablename = '" + table + "' ORDER BY p.indexname"
        )
        statements[prefix + "_defaults"] = (
            "SELECT column_name || ':' || column_default "
            "FROM information_schema.columns WHERE table_schema = 'public' "
            "AND table_name = '" + table + "' AND column_default IS NOT NULL "
            "ORDER BY column_name"
        )
    statements["handoff_byte_types"] = (
        "SELECT table_name || ':' || column_name || ':' || data_type "
        "FROM information_schema.columns WHERE table_schema = 'public' AND "
        "((table_name = 'acquisition_receipts' AND column_name = 'bytes_received') "
        "OR (table_name = 'media_asset_validations' AND column_name = 'size_bytes')) "
        "ORDER BY table_name"
    )
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
    verify_ai_settings_schema()
    if database_query("acquisition_table"):
        raise Blocked("acquisition jobs table exists beyond reviewed 0064 migration")


def verify_ai_settings_schema() -> None:
    if (database_query("table") != "application_ai_settings"
        or database_query("columns").splitlines() != [
            "key:character varying:NO:64", "value:text:NO:"]
        or database_query("primary") != "key"
        or database_query("settings_constraints") !=
            "application_ai_settings_pkey:p:PRIMARY KEY (key)"
        or database_query("settings_indexes") !=
            "application_ai_settings_pkey:CREATE UNIQUE INDEX application_ai_settings_pkey "
            "ON public.application_ai_settings USING btree (key)"
        or database_query("settings_defaults")):
        raise Blocked("approved AI settings table schema differs from reviewed migration")


def verify_acquisition_jobs_schema(handoff: bool = False) -> None:
    if database_query("acquisition_table") != "acquisition_jobs":
        raise Blocked("approved acquisition jobs table missing")
    expected_columns = [
        "id:uuid:NO:", "receipt_id:uuid:NO:", "channel_profile_id:uuid:NO:",
        "status:character varying:NO:32", "generation:integer:NO:",
        "max_bytes:bigint:NO:", "bytes_received:bigint:NO:",
        "heartbeat_sequence:integer:NO:", "worker_principal:character varying:YES:64",
        "claim_id:uuid:YES:", "lease_expires_at:timestamp with time zone:YES:",
        "last_heartbeat_at:timestamp with time zone:YES:", "phase:character varying:NO:32",
        "pending_result:json:NO:", "failure_code:character varying:YES:64",
        "created_at:timestamp with time zone:NO:", "updated_at:timestamp with time zone:NO:",
    ]
    if handoff:
        expected_columns += ["media_type:character varying:NO:16", "source_item_id:uuid:YES:"]
    if database_query("acquisition_columns").splitlines() != expected_columns:
        raise Blocked("approved acquisition jobs column schema differs from reviewed migration")
    constraints = list(ACQUISITION_CONSTRAINTS)
    if handoff:
        constraints = [c.replace(
            "varying]::text[]))", "varying, 'completed'::character varying]::text[]))")
                       if c.startswith("ck_acquisition_job_status:") else c for c in constraints]
    if database_query("acquisition_constraints").splitlines() != constraints:
        raise Blocked("approved acquisition jobs constraints differ from reviewed migration")
    if database_query("acquisition_indexes").splitlines() != [
        'acquisition_jobs_pkey:CREATE UNIQUE INDEX acquisition_jobs_pkey ON '
        'public.acquisition_jobs USING btree (id)',
        'ix_acquisition_jobs_channel_profile_id:CREATE INDEX '
        'ix_acquisition_jobs_channel_profile_id ON public.acquisition_jobs USING '
        'btree (channel_profile_id)',
        'ix_acquisition_jobs_status:CREATE INDEX ix_acquisition_jobs_status ON '
        'public.acquisition_jobs USING btree (status)',
        'uq_acquisition_job_receipt:CREATE UNIQUE INDEX uq_acquisition_job_receipt '
        'ON public.acquisition_jobs USING btree (receipt_id)',
    ] or database_query("acquisition_defaults").splitlines() != (
        ["created_at:now()"] + (["media_type:'video'::character varying"] if handoff else [])
        + ["updated_at:now()"]
    ):
        raise Blocked("approved acquisition jobs indexes/defaults differ from reviewed migration")


def verify_handoff_schema() -> None:
    verify_ai_settings_schema()
    verify_acquisition_jobs_schema(handoff=True)
    if (database_query("transfer_table") != "acquisition_transfers"
        or database_query("transfer_columns").splitlines() != [
            "job_id:uuid:NO:", "generation:integer:NO:", "sha256:character varying:NO:64",
            "size_bytes:bigint:NO:", "upload_id:text:NO:", "parts:json:NO:",
            "completed:boolean:NO:",
        ]
        or database_query("transfer_constraints").splitlines() != [
            "acquisition_transfers_job_id_fkey:f:FOREIGN KEY (job_id) "
            "REFERENCES acquisition_jobs(id)",
            "acquisition_transfers_pkey:p:PRIMARY KEY (job_id)",
            "ck_acquisition_transfer_size:c:CHECK (size_bytes > 0)",
        ]
        or database_query("transfer_indexes") !=
            "acquisition_transfers_pkey:CREATE UNIQUE INDEX acquisition_transfers_pkey "
            "ON public.acquisition_transfers USING btree (job_id)"
        or database_query("transfer_defaults")
        or database_query("handoff_byte_types").splitlines() != [
            "acquisition_receipts:bytes_received:bigint",
            "media_asset_validations:size_bytes:bigint",
        ]):
        raise Blocked("approved acquisition handoff schema differs from reviewed migration")


def database_release_preflight(migration: bool, migration_policy: str = "refuse") -> None:
    """Check the actual database, including already-applied reviewed additions."""
    if migration_policy not in MIGRATION_POLICIES:
        raise Blocked("unrecognized migration approval")
    if not migration and migration_policy != "refuse":
        raise Blocked("migration approval supplied without migration")
    if migration_policy == "approve-additive-0064":
        database_migration_preflight()
        if database_query("acquisition_table"):
            raise Blocked("acquisition jobs table exists before reviewed migration")
        return
    revision = database_query("revision")
    if revision == HANDOFF_REVISION:
        if migration_policy not in {"refuse", "approve-additive-through-0066"}:
            raise Blocked("0066 schema exceeds migration policy")
        verify_handoff_schema()
        return
    if migration_policy == "approve-additive-through-0066":
        if revision != LATEST_REVISION:
            raise Blocked("0066 approval requires verified 0065 base")
        verify_ai_settings_schema()
        verify_acquisition_jobs_schema()
        if database_query("transfer_table"):
            raise Blocked("acquisition transfers table already exists before migration")
        return
    if revision == LATEST_REVISION:
        verify_ai_settings_schema()
        verify_acquisition_jobs_schema()
        return
    if not migration or migration_policy != "approve-additive-through-0065":
        raise Blocked("migration-free release requires reviewed 0065 schema")
    if revision == BEFORE_REVISION:
        database_migration_preflight()
    elif revision == AFTER_REVISION:
        verify_ai_settings_schema()
    else:
        raise Blocked("database revision is outside reviewed 0063/0064/0065 boundary")
    if database_query("acquisition_table"):
        raise Blocked("acquisition jobs table already exists before migration")


def database_release_postflight(migration_policy: str = "refuse") -> None:
    if migration_policy == "approve-additive-through-0066" or (
        migration_policy == "refuse" and database_query("revision") == HANDOFF_REVISION
    ):
        if database_query("revision") != HANDOFF_REVISION:
            raise Blocked("database was not upgraded to reviewed 0066 target revision")
        verify_handoff_schema()
        return
    if migration_policy == "approve-additive-0064":
        database_migration_postflight()
        return
    if database_query("revision") != LATEST_REVISION:
        raise Blocked("database was not upgraded to reviewed 0065 target revision")
    verify_ai_settings_schema()
    verify_acquisition_jobs_schema()


def stage_images(target: str, *, single_host: bool = False) -> dict[str, str]:
    staged: dict[str, str] = {}
    for name in (*IMAGES, *(("katcha-analysis",) if single_host else ())):
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



def stop_supervisor() -> None:
    """Stop the sole Compose owner, accepting its observed SIGINT exit only.

    Attached Compose can finish stopping every service and exit 130. systemd
    records that cleanly stopped stack as failed/exit-code. Clear that specific
    result only after proving neither its process nor project containers remain.
    All other failures retain their diagnostic state and block deployment.
    """
    run("systemctl", "stop", "katcha.service", timeout=180)

    def verify_stopped() -> str:
        if run("docker", "ps", "--filter",
               "label=com.docker.compose.project=katcha-production",
               "--format", "{{.ID}}"):
            raise Blocked("old Compose supervisor did not fully stop")
        if run("systemctl", "show", "-p", "MainPID", "--value",
               "katcha.service") != "0":
            raise Blocked("production supervisor process remains active")
        return run("systemctl", "show", "-p", "ActiveState", "--value",
                   "katcha.service")

    state = verify_stopped()
    if state == "failed":
        result = run("systemctl", "show", "-p", "Result", "--value",
                     "katcha.service")
        status = run("systemctl", "show", "-p", "ExecMainStatus", "--value",
                     "katcha.service")
        if (result, status) != ("exit-code", "130"):
            raise Blocked("unexpected production supervisor stop failure")
        run("systemctl", "reset-failed", "katcha.service")
        # Resetting the unit must not conceal a concurrent restart or live stack.
        state = verify_stopped()
    if state != "inactive":
        raise Blocked("production supervisor is not inactive after stop")


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
        or migration_policy not in MIGRATION_POLICIES):
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
    database_release_preflight(migration, migration_policy)
    target_has_handoff = any(
        item.get("name") == Path(HANDOFF_FILE).name
        for item in github("/contents/migrations/versions?ref=" + target)
    )
    if not migration and target_has_handoff and database_query("revision") != HANDOFF_REVISION:
        raise Blocked("migration-free target requires reviewed 0066 schema")
    run("git", "-C", str(REPO), "fetch", "origin", "main", timeout=60)
    target_source = run("git", "-C", str(REPO), "show",
                        target + ":deploy/docker-compose.production.yml")
    local_profile = single_host_profile(target_source)
    current_source = run("git", "-C", str(REPO), "show",
                         current + ":deploy/docker-compose.production.yml")
    first_activation = local_profile and not single_host_profile(current_source)
    # Preserve idle recurring schedulers, whose heavy work starts new child
    # workflows. All legacy media executions must finish before first activation.
    before = (temporal_inventory(policy, schedulers_only=True) if first_activation
              else temporal_inventory(policy))
    staged = stage_images(target, single_host=True) if local_profile else stage_images(target)
    backup_and_restore()
    # All operations above are preparation-only. Recheck authority, workflow
    # policy and verified release immediately before the maintenance window.
    authority()
    current_inventory = (temporal_inventory(policy, schedulers_only=True) if first_activation
                         else temporal_inventory(policy))
    if current_inventory != before:
        raise Blocked("running Temporal executions changed during backup/preparation")
    if eligible(current, target, migration_policy) != migration:
        raise Blocked("migration classification changed during deployment review")
    database_release_preflight(migration, migration_policy)
    # The pinned candidate is allowed to be behind newer main merges. Both
    # git checks enforce ancestry; neither switches to the mutable head.
    run("git", "-C", str(REPO), "merge-base", "--is-ancestor", target, "origin/main")
    run("git", "-C", str(REPO), "merge-base", "--is-ancestor", current, target)
    for path, blob in (({**REVIEWED_MIGRATIONS, HANDOFF_FILE: HANDOFF_BLOB}.items()
                       if migration_policy == "approve-additive-through-0066"
                       else REVIEWED_MIGRATIONS.items())
                       if migration_policy != "approve-additive-0064"
                       else {MIGRATION_FILE: MIGRATION_BLOB}.items()):
        if run("git", "-C", str(REPO), "rev-parse", target + ":" + path) != blob:
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
        stop_supervisor()
        lease_fd = acquire_supervisor_lease()
        try:
            run("git", "-C", str(REPO), "merge", "--ff-only", target, timeout=60)
            update_pin(target, backup)
        finally:
            os.close(lease_fd)
        # The successor must obtain the same private lease itself.
        run("systemctl", "start", "katcha.service", timeout=900)
        healthy(target, staged=staged)
        database_release_postflight(migration_policy)
        resumed = workflows("approved-resume")
        recovered = verify_temporal_recovery(before)
        outcome = {"status": "DEPLOYED", "sha": target, "previous_sha": current,
                   "predeploy_running_workflows": len(before),
                   "postdeploy_running_workflows": resumed,
                   "predeploy_workflow_recovery": recovered,
                   "migration_revision": (
                       AFTER_REVISION if migration_policy == "approve-additive-0064"
                       else HANDOFF_REVISION if migration_policy == "approve-additive-through-0066"
                       else LATEST_REVISION) if migration else "unchanged",
                   "migration_rollback": "FORWARD_ONLY" if migration else "not_applicable",
                   "public_browser_acceptance": "REQUIRED"}
    except Exception as exc:
        # An upgraded database contains a revision unknown to the previous
        # image. Never blindly restart an old image after a migration-bearing
        # change; preserve data and require a supervised forward repair.
        if migration and started_change:
            outcome = {
                "status": "RECOVERY_REQUIRED", "sha": target,
                "previous_sha": current, "migration": (AFTER_REVISION
                    if migration_policy == "approve-additive-0064"
                    else HANDOFF_REVISION if migration_policy == "approve-additive-through-0066"
                    else LATEST_REVISION),
                "reason": "forward-only migration release needs supervised repair",
            }
            (record_dir / "result.json").write_text(json.dumps(outcome) + "\n")
            raise Blocked("forward-only migration rollout failed; old image rollback prohibited") from exc
        if started_change:
            try:
                stop_supervisor()
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
    revision = database_query("revision")
    if revision == BEFORE_REVISION:
        database_migration_preflight()
        if database_query("acquisition_table"):
            raise Blocked("acquisition jobs table already exists before migration")
        schema_state = "requires_approved_additive_through_0065"
    elif revision == AFTER_REVISION:
        verify_ai_settings_schema()
        if database_query("acquisition_table"):
            raise Blocked("acquisition jobs table already exists before migration")
        schema_state = "0064_verified_requires_approved_additive_through_0065"
    elif revision == HANDOFF_REVISION:
        database_release_postflight()
        schema_state = "0066_already_applied_and_verified"
    elif revision == LATEST_REVISION:
        database_release_postflight()
        schema_state = "0065_already_applied_and_verified"
    else:
        raise Blocked("database revision is outside reviewed 0063/0064/0065 boundary")
    open_executions = temporal_inventory("approved-resume")
    return {
        "status": "REVIEW_REQUIRED",
        "migration": LATEST_REVISION,
        "database_start_revision": revision,
        "database_schema_state": schema_state,
        "temporal_running_count": len(open_executions),
        "temporal_pending_activity_check": "PASSED",
        "note": "Read-only preflight. Not a deployment authorization.",
    }


def plan_release(target: str) -> dict[str, Any]:
    """Read-only release-specific diagnosis of the current OCI production host.

    The plan computes a *candidate* migration policy from the exact checked
    comparison and actual database revision, then invokes the existing strict
    release/migration validators. It never grants migration/workflow approval.
    """
    if not SHA.fullmatch(target):
        raise Blocked("target must be an exact 40-character release SHA")
    state = readiness()  # Attests host/runtime/schema and every Temporal execution.
    old, _ = pinned(ENV)
    if old == target:
        accepted_main_ancestry(target)
        return {
            "status": "CURRENT", "current_sha": old, "target_sha": target,
            "database_revision": state["database_start_revision"],
            "temporal_running_count": state["temporal_running_count"],
            "deployment_authorized": False,
        }

    comparison = github("/compare/" + old + "..." + target)
    if not isinstance(comparison, dict):
        raise Blocked("candidate comparison unavailable")
    # Never accept GitHub's 300-file truncated comparison or a huge commit
    # range, even when a particular migration filename happens to be visible.
    files = comparison.get("files")
    commits = comparison.get("total_commits")
    if (comparison.get("status") != "ahead" or comparison.get("behind_by") != 0
        or not isinstance(comparison.get("merge_base_commit"), dict)
        or comparison["merge_base_commit"].get("sha") != old
        or type(commits) is not int or not 0 < commits <= 250
        or not isinstance(files, list) or not 0 < len(files) < 300):
        raise Blocked("candidate ancestry or comparison exceeds reviewed limits")
    changed_migrations = []
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
            raise Blocked("candidate changed-file metadata malformed")
        name = item["filename"]
        if name.startswith(("migrations/", "alembic/")) or name == "alembic.ini":
            changed_migrations.append((name, item.get("status")))

    start_revision = state["database_start_revision"]
    if not changed_migrations:
        policy = "refuse"
    elif changed_migrations == [(HANDOFF_FILE, "added")]:
        if start_revision != LATEST_REVISION:
            raise Blocked("0066-only release requires verified 0065 production database")
        policy = "approve-additive-through-0066"
    elif (start_revision in {BEFORE_REVISION, AFTER_REVISION}
          and all(kind == "added" for _, kind in changed_migrations)
          and set(name for name, _ in changed_migrations)
          <= set(REVIEWED_MIGRATIONS)
          and MIGRATION_0065_FILE in {name for name, _ in changed_migrations}):
        policy = "approve-additive-through-0065"
    elif changed_migrations == [(MIGRATION_FILE, "added")] and (
        start_revision == BEFORE_REVISION
    ):
        policy = "approve-additive-0064"
    else:
        raise Blocked("migration chain is not a supported single release step")

    migration = eligible(old, target, policy)
    database_release_preflight(migration, policy)
    inventory = github("/contents/migrations/versions?ref=" + target)
    if not isinstance(inventory, list):
        raise Blocked("target migration inventory unavailable")
    if not migration and any(
        item.get("name") == Path(HANDOFF_FILE).name
        for item in inventory if isinstance(item, dict)
    ) and start_revision != HANDOFF_REVISION:
        raise Blocked("target includes 0066 but production is below verified 0066")

    accepted_main_ancestry(target)

    active = state["temporal_running_count"]
    return {
        "status": "REVIEW_REQUIRED",
        "current_sha": old, "target_sha": target,
        "commits_behind": commits, "changed_files": len(files),
        "commit_limit": 250, "changed_file_limit_exclusive": 300,
        "database_revision": start_revision,
        "migration_files": [name for name, _ in changed_migrations],
        "candidate_migration_policy": policy,
        "schema_change": migration,
        "temporal_running_count": active,
        "temporal_pending_activity_check": state["temporal_pending_activity_check"],
        "workflow_review": (
            "active_executions_require_review"
            if active else "no_running_executions_observed"
        ),
        "default_workflow_policy": "refuse",
        "deployment_authorized": False,
        "next_action": (
            "review_active_workflows_and_forward_only_migration_before_approval"
            if active or migration else "review_maintenance_window_and_backup_restore"
        ),
    }



def _diagnostic_check(
    name: str, status: str, detail: str, next_action: str | None = None,
    **evidence: Any,
) -> dict[str, Any]:
    """Consistent read-only operator evidence; never include credential values."""
    result: dict[str, Any] = {"name": name, "status": status, "detail": detail}
    if next_action:
        result["next_action"] = next_action
    if evidence:
        result["evidence"] = evidence
    return result


def deployment_control_diagnostics() -> list[dict[str, Any]]:
    """Inspect local deploy controls without enabling a gate or reading a signing key."""
    checks: list[dict[str, Any]] = []
    if GATE.is_symlink():
        checks.append(_diagnostic_check(
            "host_deployment_gate", "BLOCKED", "Host gate is a symlink.",
            "Investigate the gate path; do not authorize deployment.",
        ))
    elif not GATE.exists():
        checks.append(_diagnostic_check(
            "host_deployment_gate", "PASS", "Host gate is OFF (normal manual state).",
        ))
    elif not GATE.is_file():
        checks.append(_diagnostic_check(
            "host_deployment_gate", "BLOCKED", "Host gate is not a regular file.",
            "Investigate the host gate path before requesting deployment.",
        ))
    else:
        gate_stat = GATE.stat()
        if gate_stat.st_uid != 0 or gate_stat.st_mode & 0o077:
            checks.append(_diagnostic_check(
                "host_deployment_gate", "BLOCKED", "Host gate owner or permissions unsafe.",
                "Restore the root-only gate state using the approved runbook.",
            ))
        elif GATE.read_text(encoding="utf-8").strip() == (
            "KATCHA_PRODUCTION_DEPLOY_ENABLED=true"
        ):
            checks.append(_diagnostic_check(
                "host_deployment_gate", "REVIEW_REQUIRED", "Host gate is ON.",
                "Confirm this is an approved one-shot window; otherwise turn it OFF.",
            ))
        else:
            checks.append(_diagnostic_check(
                "host_deployment_gate", "BLOCKED", "Host gate contents are unexpected.",
                "Inspect the host deployment gate before continuing.",
            ))

    if SIGNING_KEY.is_symlink() or not SIGNING_KEY.is_file():
        checks.append(_diagnostic_check(
            "signing_key_metadata", "BLOCKED", "Root-only signing key file is missing or unsafe.",
            "Restore the existing signing key through the documented recovery path.",
        ))
    else:
        key_stat = SIGNING_KEY.stat()
        if (key_stat.st_uid != 0 or stat.S_IMODE(key_stat.st_mode) != 0o600
            or key_stat.st_size != 65):
            checks.append(_diagnostic_check(
                "signing_key_metadata", "BLOCKED",
                "Signing key owner, mode or length does not match expected metadata.",
                "Verify the existing key metadata without printing or regenerating it.",
            ))
        else:
            checks.append(_diagnostic_check(
                "signing_key_metadata", "PASS",
                "Root-only key metadata matches. HMAC equivalence is not tested.",
            ))

    try:
        timer = subprocess.run(
            ["systemctl", "is-enabled", "katcha-production-deploy-poll.timer"],
            capture_output=True, text=True, timeout=5,
        )
        timer_state = timer.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        timer_state = ""
    if timer_state == "disabled":
        checks.append(_diagnostic_check(
            "automatic_deploy_timer", "PASS", "Automatic deployment polling is disabled.",
        ))
    elif timer_state == "enabled":
        checks.append(_diagnostic_check(
            "automatic_deploy_timer", "REVIEW_REQUIRED",
            "Automatic deployment polling is enabled, unlike manual baseline.",
            "Review and disable the poll timer using the manual deployment runbook.",
        ))
    else:
        checks.append(_diagnostic_check(
            "automatic_deploy_timer", "BLOCKED", "Deployment timer state could not be verified.",
            "Check systemctl is-enabled for the production deployment poll timer.",
        ))

    if TOOL_SOURCE.is_symlink() or not TOOL_SOURCE.is_file():
        checks.append(_diagnostic_check(
            "installed_tool_version", "UNKNOWN", "Tool installation source is not recorded.",
            "Refresh the root-owned deployment tools using the reviewed installer.",
        ))
    else:
        pin_stat = TOOL_SOURCE.stat()
        if pin_stat.st_uid != 0 or pin_stat.st_mode & 0o077 or pin_stat.st_size > 64:
            checks.append(_diagnostic_check(
                "installed_tool_version", "BLOCKED", "Installed-tool manifest is unsafe.",
                "Reinstall root-owned deployment tooling using the reviewed installer.",
            ))
        else:
            tool_sha = TOOL_SOURCE.read_text(encoding="ascii").strip()
            if not SHA.fullmatch(tool_sha):
                checks.append(_diagnostic_check(
                    "installed_tool_version", "BLOCKED", "Installed-tool version is invalid.",
                    "Reinstall deployment tooling from verified accepted main.",
                ))
            else:
                checks.append(_diagnostic_check(
                    "installed_tool_version", "PASS",
                    "Installed tooling source SHA recorded; application version may differ.",
                    source_sha=tool_sha,
                ))
    return checks


def diagnose_release(target: str) -> dict[str, Any]:
    """One read-only diagnostic report; never confuse a review with authorization."""
    report: dict[str, Any] = {
        "status": "BLOCKED",
        "target_sha": target,
        "checked_at": dt.datetime.now(dt.UTC).isoformat(),
        "deployment_authorized": False,
        "checks": [],
        "next_actions": [],
    }
    checks = report["checks"]
    if not SHA.fullmatch(target):
        checks.append(_diagnostic_check(
            "target_sha", "BLOCKED", "An exact lowercase 40-character SHA is required.",
            "Copy the verified exact release SHA from GitHub main.",
        ))
        return report
    checks.append(_diagnostic_check("target_sha", "PASS", "Exact release SHA supplied."))
    try:
        if os.geteuid() != 0 or run("hostname") != "katcha-prod-primary-001":
            raise Blocked("This command requires root on the verified OCI production primary")
    except Blocked:
        checks.append(_diagnostic_check(
            "host_identity", "BLOCKED", "Not the verified root-owned OCI production primary.",
            "Connect to katcha-prod-primary-001 using the existing Bastion procedure.",
        ))
        return report
    checks.append(_diagnostic_check("host_identity", "PASS", "Verified OCI primary and root."))

    try:
        checks.extend(deployment_control_diagnostics())
    except (OSError, UnicodeError, ValueError, TypeError):
        checks.append(_diagnostic_check(
            "deployment_controls", "BLOCKED", "Could not inspect deployment control metadata.",
            "Inspect root-owned deployment controls without printing secrets.",
        ))

    try:
        ready = readiness()
    except Blocked as exc:
        checks.append(_diagnostic_check(
            "production_readiness", "BLOCKED", str(exc),
            "Resolve host/runtime/database/Temporal readiness before release planning.",
        ))
    except (OSError, ValueError, KeyError, TypeError):
        checks.append(_diagnostic_check(
            "production_readiness", "BLOCKED", "Host readiness inspection failed.",
            "Inspect the production supervisor, database and Temporal diagnostics.",
        ))
    else:
        checks.append(_diagnostic_check(
            "production_readiness", "PASS",
            "Runtime, migration base and Temporal pending-activity checks completed.",
            database_revision=ready["database_start_revision"],
            temporal_running_count=ready["temporal_running_count"],
        ))
        try:
            planned = plan_release(target)
        except Blocked as exc:
            checks.append(_diagnostic_check(
                "release_plan", "BLOCKED", str(exc),
                "Inspect the independent release verifier and candidate comparison.",
            ))
        except (OSError, ValueError, KeyError, TypeError):
            checks.append(_diagnostic_check(
                "release_plan", "BLOCKED", "Release-specific planning failed.",
                "Inspect the release verifier, migration inventory and host readiness.",
            ))
        else:
            report["release_plan"] = planned
            checks.append(_diagnostic_check(
                "release_plan", "PASS",
                "Exact release preflight completed; approval has not been granted.",
            ))
            if planned["status"] == "REVIEW_REQUIRED":
                checks.append(_diagnostic_check(
                    "backup_restore_review", "REVIEW_REQUIRED",
                    "Recent R2 backup and isolated restore evidence are not attested by this doctor.",
                    "Verify current protected backup/restore receipts before signed approval.",
                ))
                checks.append(_diagnostic_check(
                    "github_request_approval", "REVIEW_REQUIRED",
                    "No GitHub protected approval or signed request is created by diagnosis.",
                    "After reviewing migration and workflows, authorize an exact-SHA request "
                    "using the manual runbook.",
                ))
                if planned.get("schema_change"):
                    checks.append(_diagnostic_check(
                        "migration_review", "REVIEW_REQUIRED",
                        "Forward-only schema migration needs an explicit operator decision.",
                        "Review the proposed migration and restore evidence before approval.",
                        candidate_policy=planned["candidate_migration_policy"],
                    ))
                if planned.get("temporal_running_count", 0):
                    checks.append(_diagnostic_check(
                        "temporal_workflow_review", "REVIEW_REQUIRED",
                        "Running Temporal executions require an explicit restart decision.",
                        "Review workflow identities, pending state and replay/idempotence.",
                        running_count=planned["temporal_running_count"],
                    ))
    report["next_actions"] = [
        {"check": item["name"], "instruction": item["next_action"]}
        for item in checks if "next_action" in item
    ]
    if any(item["status"] == "BLOCKED" for item in checks):
        report["status"] = "BLOCKED"
    elif report.get("release_plan", {}).get("status") == "CURRENT":
        report["status"] = "CURRENT"
    else:
        report["status"] = "REVIEW_REQUIRED"
    return report


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--diagnose-release":
        report = diagnose_release(sys.argv[2])
        print(json.dumps(report, sort_keys=True))
        return 2 if report["status"] == "BLOCKED" else 0
    if len(sys.argv) == 3 and sys.argv[1] == "--plan-release":
        try:
            print(json.dumps(plan_release(sys.argv[2]), sort_keys=True))
            return 0
        except (Blocked, OSError, ValueError, KeyError, TypeError) as exc:
            print(json.dumps({"status": "BLOCKED", "reason": str(exc)}))
            return 2
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
