"""Persistent host operations monitor and bounded remediation bot (stdlib only).

The ledger is private. Only allowlisted summaries reach the API or Telegram.
No release approval, production restart, migration or infrastructure write lives here.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib.machinery import SourceFileLoader
from pathlib import Path
from typing import Any

ROOT = Path("/srv/katcha/operations")
ENV = Path("/etc/katcha/katcha.env")
DEPLOYER = Path("/usr/local/sbin/katcha-production-deploy")
NOTIFIER = Path("/usr/local/sbin/katcha-production-deploy-notify")
DEPLOY_LOCK = Path("/run/lock/katcha-production-deployment.lock")
HOST = "katcha-prod-primary-001"
DEADLINE: float | None = None
HEALTH_URL = "https://app.katcha.stream/operations#operations-health"
# A sliding failed-execution lookback expiring is not evidence of recovery.
TEMPORAL_QUIET_SECONDS = 6 * 3600
TEMPORAL_MAX_GAP_SECONDS = 15 * 60
SAFETY = {
    "backup": (
        "katcha-backup.service",
        2 * 3600,
        r"Katcha PostgreSQL backup completed: [A-Za-z0-9-]+",
    ),
    "restore": (
        "katcha-restore-test.service",
        8 * 86400,
        r"Katcha disaster restore test passed for 3 databases\.",
    ),
}


@dataclass(frozen=True)
class Signal:
    key: str
    label: str
    status: str
    detail: str
    action: str = ""
    completed_at: float | None = None
    fingerprint: str = ""


class ProbeError(RuntimeError):
    """Only controlled, non-secret tool names and failure codes belong here."""


def command(*args: str, timeout: int = 20, accept_failure: bool = False) -> str:
    if DEADLINE is not None:
        remaining = DEADLINE - time.monotonic()
        if remaining < 1:
            raise ProbeError("Operations probe budget exhausted; retry on the next observation.")
        timeout = min(timeout, remaining)
    tool = Path(args[0]).name
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ProbeError(tool + " exceeded its diagnostic time limit.") from None
    except OSError as exc:
        raise ProbeError(tool + " could not start (system error " + str(exc.errno) + ").") from None
    if result.returncode and not accept_failure:
        # Neither stderr nor command arguments are safe diagnostics.
        raise ProbeError(tool + " exited with status " + str(result.returncode) + ".")
    return result.stdout.strip()


def trusted_file(path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    meta = path.stat()
    return meta.st_uid == 0 and not meta.st_mode & 0o022


def installed_notifier() -> Any:
    if not trusted_file(NOTIFIER):
        raise RuntimeError("Installed notifier is unavailable")
    loader = SourceFileLoader("katcha_ops_notifier", str(NOTIFIER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def properties(unit: str) -> dict[str, str]:
    raw = command(
        "systemctl",
        "show",
        unit,
        "--property="
        + ",".join(
            (
                "LoadState",
                "ActiveState",
                "Result",
                "ExecMainStatus",
                "UnitFileState",
            )
        ),
    )
    result = dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)
    if result.get("LoadState") != "loaded":
        raise ProbeError("Required systemd unit is missing: " + unit)
    return result


def safety_signal(kind: str, now: float) -> Signal:
    unit, max_age, marker = SAFETY[kind]
    label = "Protected backups" if kind == "backup" else "Isolated restore tests"
    state = properties(unit)
    timer = properties(unit.replace(".service", ".timer"))
    if timer.get("UnitFileState") != "enabled" or timer.get("ActiveState") != "active":
        return Signal(
            kind,
            label,
            "critical",
            "Scheduled safety checks are disabled.",
            "Review and restore the existing safety timer.",
        )
    if state.get("ActiveState") == "activating":
        # Still inspect last positive completion: an active job is not success.
        running = True
    else:
        running = False
        if state.get("Result") != "success" or state.get("ExecMainStatus") != "0":
            return Signal(
                kind,
                label,
                "critical",
                "The latest safety job failed.",
                "The remediation bot will check and retry once when safe.",
            )
    raw = command(
        "journalctl",
        "-u",
        unit,
        "--since",
        "-9 days",
        "--no-pager",
        "--grep",
        "^" + marker + "$",
        "-n",
        "1",
        "-o",
        "json",
    )
    try:
        receipt = json.loads(raw)
        stamp = int(receipt["__REALTIME_TIMESTAMP"]) / 1_000_000
        if (
            receipt.get("_SYSTEMD_UNIT") != unit
            or not re.fullmatch(marker, receipt.get("MESSAGE", ""))
            or stamp > now + 300
            or stamp <= 0
        ):
            raise ValueError("Invalid completion evidence")
    except (ValueError, KeyError, TypeError):
        return Signal(
            kind,
            label,
            "unknown",
            "No verified completion is available.",
            "The remediation bot will check and run a safety job when safe.",
        )
    age = now - stamp
    detail = "Last verified completion: " + datetime.fromtimestamp(stamp, UTC).isoformat()
    if running:
        detail += ". A new check is running."
    if age > max_age:
        return Signal(
            kind,
            label,
            "critical",
            detail + ". Completion is overdue.",
            "The remediation bot will retry once when safe.",
        )
    return Signal(kind, label, "healthy", detail, completed_at=stamp)


# Error strings are inspected only in process memory. Never include the input
# values, raw Temporal history, IDs or stack traces in the ledger or Telegram.
TEMPORAL_CAUSES = (
    ("DISK_FULL", r"enospc|no space left on device|disk full|filesystem full",
     "Check root/data volume space and inode usage; preserve stored data."),
    ("OUT_OF_MEMORY", r"out of memory|oomkilled|oom killed|memoryerror",
     "Check worker OOMs, container limits and host memory pressure."),
    ("RATE_LIMIT", r"rate.?limit|too many requests|quota.?exceeded|http.?429|\b429\b",
     "Inspect the affected provider quota and bounded retry/backoff settings."),
    ("AUTHENTICATION", r"unauthorized|unauthenticated|invalid.?token|invalid.?credential|"
     r"expired.?token|authentication failed|http.?401|\b401\b",
     "Check the relevant provider credential's validity and authorized scope."),
    ("PERMISSION", r"permission denied|access denied|forbidden|http.?403|\b403\b",
     "Check the failing service's access policy and filesystem permissions."),
    ("CODE_COMPATIBILITY", r"non.?deterministic|not registered|unknown workflow type|"
     r"workflow task failed cause non deterministic",
     "Inspect worker workflow registration and replay compatibility."),
    ("DEPENDENCY_MISSING", r"modulenotfounderror|importerror|no module named|"
     r"command not found|executable file not found",
     "Inspect installed worker dependencies and deployed image versions."),
    ("SOURCE_UNAVAILABLE", r"not found|http.?404|\b404\b|source unavailable",
     "Verify the source/provider endpoint and source availability."),
    ("NETWORK", r"connection refused|network unreachable|name resolution|"
     r"getaddrinfo|dns failed|tls handshake|http.?50[234]|\b50[234]\b",
     "Inspect provider reachability, DNS, egress and service availability."),
    ("TIMEOUT", r"timed out|timeout|deadline exceeded",
     "Inspect worker capacity, activity timeouts and dependent services."),
)
TEMPORAL_ACTIONS = {kind: action for kind, _, action in TEMPORAL_CAUSES}
TEMPORAL_ACTIONS.update({
    "HISTORY_UNAVAILABLE": "Inspect the exact failed execution on the Temporal host.",
    "UNCLASSIFIED": "Inspect the private Temporal event history for the terminal failure.",
    "ACTIVITY_FAILURE": "Inspect failing activity history and its worker logs.",
    "ACTIVITY_TIMEOUT": "Inspect activity timeouts, heartbeat settings and worker capacity.",
})


def classify_temporal_failure(failure: Any, *, event_cause: str = "") -> str:
    """Classify without ever returning unchecked failure content."""
    if "NON_DETERMINISTIC" in event_cause.upper():
        return "CODE_COMPATIBILITY"
    fragments: list[str] = []
    current = failure
    for _ in range(5):
        if not isinstance(current, dict):
            break
        for key in ("message", "source"):
            value = current.get(key)
            if isinstance(value, str):
                fragments.append(value[:512])
        info = current.get("applicationFailureInfo")
        if isinstance(info, dict) and isinstance(info.get("type"), str):
            fragments.append(info["type"][:160])
        if isinstance(current.get("timeoutFailureInfo"), dict):
            return "ACTIVITY_TIMEOUT"
        current = current.get("cause")
    sample = " ".join(fragments)
    for kind, pattern, _ in TEMPORAL_CAUSES:
        if re.search(pattern, sample, flags=re.IGNORECASE):
            return kind
    if isinstance(failure, dict):
        if isinstance(failure.get("activityFailureInfo"), dict):
            return "ACTIVITY_FAILURE"
        if fragments:
            return "UNCLASSIFIED"
    return "HISTORY_UNAVAILABLE"


def classify_temporal_history(history: Any) -> str:
    """Accept Temporal CLI's JSON event-history envelope, not arbitrary payloads."""
    events = history.get("events") if isinstance(history, dict) else None
    if not isinstance(events, list) or len(events) > 20_000:
        return "HISTORY_UNAVAILABLE"
    for item in reversed(events):
        if not isinstance(item, dict):
            continue
        attributes = item.get("workflowExecutionFailedEventAttributes")
        if isinstance(attributes, dict):
            return classify_temporal_failure(attributes.get("failure"))
    for item in reversed(events):
        if not isinstance(item, dict):
            continue
        attributes = item.get("workflowTaskFailedEventAttributes")
        if isinstance(attributes, dict):
            return classify_temporal_failure(
                attributes.get("failure"), event_cause=str(attributes.get("cause", ""))[:128]
            )
    return "HISTORY_UNAVAILABLE"


def temporal_failure_cause(
    prefix: tuple[str, ...], common: tuple[str, ...], execution: Any
) -> str:
    """Best-effort bounded read-only inspection of one *closed* failed run."""
    if not isinstance(execution, dict):
        return "HISTORY_UNAVAILABLE"
    wid, rid = execution.get("workflowId"), execution.get("runId")
    if (
        not isinstance(wid, str) or not isinstance(rid, str)
        or not (0 < len(wid) <= 255) or not (0 < len(rid) <= 128)
    ):
        return "HISTORY_UNAVAILABLE"
    try:
        raw = command(
            *prefix, "show", *common, "--workflow-id", wid, "--run-id", rid, timeout=8
        )
        if len(raw) > 2_000_000:
            return "HISTORY_UNAVAILABLE"
        return classify_temporal_history(json.loads(raw))
    except (ProbeError, ValueError, TypeError):
        return "HISTORY_UNAVAILABLE"


def temporal_cause_fingerprint(
    failure_types: dict[str, int], causes: list[str], *, long_pending: bool
) -> str:
    """Stable ID for a group of safe type/cause categories, excluding raw errors."""
    safe_types = sorted(
        name for name in failure_types
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", name)
    )
    safe_causes = sorted(set(c for c in causes if c in TEMPORAL_ACTIONS))
    data = json.dumps(
        {"types": safe_types, "causes": safe_causes, "long_pending": long_pending},
        sort_keys=True,
    )
    return hashlib.sha256(data.encode("utf-8")).hexdigest()[:24]


PENDING_ACTIVITY_ATTENTION_SECONDS = 4 * 3600


def pending_activity_age(activity: Any, now: float) -> float | None:
    """Bounded read of Temporal's original scheduledTime, not untrusted messages."""
    if not isinstance(activity, dict):
        return None
    raw = activity.get("scheduledTime")
    if not isinstance(raw, str) or len(raw) > 64:
        return None
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            return None
        age = now - stamp.timestamp()
    except (ValueError, OverflowError, OSError):
        return None
    return age if 0 <= age <= 366 * 86400 else None


def temporal_signal(*, now: float | None = None) -> Signal:
    """Show bounded failure categories and useful next actions, never raw errors."""
    prefix = (
        "docker",
        "exec",
        "katcha-production-temporal-1",
        "temporal",
        "--output",
        "json",
        "workflow",
    )
    common = ("--address", "temporal:7233", "--namespace", "default")
    now = time.time() if now is None else now
    failed = json.loads(
        command(
            *prefix,
            "list",
            *common,
            "--query",
            'ExecutionStatus="Failed" AND CloseTime > "'
            + datetime.fromtimestamp(now - 3600, UTC).isoformat()
            + '"',
            "--limit",
            "101",
        )
    )
    if not isinstance(failed, list) or len(failed) > 100:
        raise ProbeError("Temporal failed workflow inventory is incomplete (limit 100).")
    failed_types: dict[str, int] = {}
    for item in failed:
        if not isinstance(item, dict):
            raise ProbeError("Temporal failed workflow inventory contains invalid records.")
        kind = item.get("type") or item.get("workflowType")
        name = kind.get("name") if isinstance(kind, dict) else None
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", name):
            name = "unidentified"
        failed_types[name] = failed_types.get(name, 0) + 1

    rows = json.loads(
        command(*prefix, "list", *common, "--query", 'ExecutionStatus="Running"', "--limit", "101")
    )
    if not isinstance(rows, list) or len(rows) > 100:
        raise ProbeError("Temporal running workflow inventory is incomplete (limit 100).")
    pending = 0
    failing = 0
    long_pending = 0
    oldest_pending = 0.0
    pending_causes: list[str] = []
    for row in rows:
        execution = row["execution"]
        wid, rid = execution["workflowId"], execution["runId"]
        if not isinstance(wid, str) or not isinstance(rid, str) or len(wid) > 255:
            raise ProbeError("Temporal running workflow identity is invalid.")
        data = json.loads(
            command(*prefix, "describe", *common, "--workflow-id", wid, "--run-id", rid)
        )
        if data["workflowExecutionInfo"]["execution"] != execution:
            raise ProbeError("Temporal workflow identity changed during inspection.")
        activities = data.get("pendingActivities") or []
        pending += len(activities)
        for activity in activities:
            if activity.get("lastFailure") and int(activity.get("attempt", 0)) >= 3:
                failing += 1
                pending_causes.append(classify_temporal_failure(activity["lastFailure"]))
            age = pending_activity_age(activity, now)
            if age is not None and age >= PENDING_ACTIVITY_ATTENTION_SECONDS:
                long_pending += 1
                oldest_pending = max(oldest_pending, age)

    if failing or failed or long_pending:
        kinds = sorted(failed_types.items(), key=lambda pair: (-pair[1], pair[0]))
        summary = ", ".join(f"{kind} ({count})" for kind, count in kinds[:4])
        if len(kinds) > 4:
            summary += f", {sum(count for _, count in kinds[4:])} other"

        # Never inspect all failing histories on the five-minute monitoring tick.
        # Most recent listed executions supply a small diagnostic sample; errors
        # inspecting one execution must not hide the failed-run count.
        sampled = min(3, len(failed))
        causes = list(pending_causes)
        causes.extend(
            temporal_failure_cause(prefix, common, item.get("execution"))
            for item in failed[:sampled]
        )
        cause_counts: dict[str, int] = {}
        for cause in causes:
            cause_counts[cause] = cause_counts.get(cause, 0) + 1
        cause_summary = ", ".join(
            f"{kind} ({count})"
            for kind, count in sorted(
                cause_counts.items(), key=lambda pair: (-pair[1], pair[0])
            )[:4]
        )
        actions = []
        for cause in causes:
            action = TEMPORAL_ACTIONS.get(cause, TEMPORAL_ACTIONS["UNCLASSIFIED"])
            if action not in actions:
                actions.append(action)
        return Signal(
            "temporal",
            "Background workflows",
            "warning",
            f"{len(failed)} failed executions in the past hour; "
            f"{failing} pending activities with repeated failures."
            + (f" {long_pending} activities pending at least 4 hours "
               f"(oldest {oldest_pending / 3600:.1f} hours)."
               if long_pending else "")
            + (f" Types: {summary}." if summary else "")
            + (f" Diagnosed causes ({sampled} sampled closed runs): {cause_summary}."
               if cause_summary else ""),
            (
                " ".join(actions[:2])
                + (" Review activity schedule/heartbeat and worker task queue; "
                   "long pending does not prove a dead worker."
                   if long_pending else "")
                + " Never replay a possibly accepted operation blindly."
            ),
            fingerprint=temporal_cause_fingerprint(
                failed_types, causes, long_pending=bool(long_pending)
            ),
        )
    return Signal(
        "temporal",
        "Background workflows",
        "healthy",
        f"{len(rows)} workflows running; {pending} activities pending. "
        "Restart compatibility is reviewed for the selected release.",
    )


def release_signals() -> list[Signal]:
    # Measure drift even when latest main is still building or has failed CI.
    from katcha.ops import production_release_observer as observer
    from katcha.ops import release_eligibility

    pins = observer.read_pins(ENV)
    old = pins["KATCHA_RELEASE_SHA"]
    split = (pins.get("KATCHA_API_RELEASE_SHA") or old) != old
    pin_signal = Signal(
        "release_pins",
        "Application release alignment",
        "decision" if split else "healthy",
        "API and worker release pins differ." if split else "Release pins agree.",
        "Review the separately promoted API before the next full rollout." if split else "",
    )
    new = observer.current_main(release_eligibility.api_get)
    if old == new:
        return [
            Signal("drift", "Release cadence", "healthy", "Production matches main."),
            Signal(
                "release", "Next release review", "healthy", "No newer main release needs review."
            ),
            pin_signal,
        ]
    diff = observer.compare_releases(release_eligibility.api_get, old, new)
    warning = diff["commits"] >= 60 or diff["changed_files"] >= 80
    return [
        Signal(
            "drift",
            "Release cadence",
            "warning" if warning else "healthy",
            f"{diff['commits']} commits and {diff['changed_files']} changed files "
            "since production.",
            "Plan a release before the 75-commit or 100-file review threshold." if warning else "",
        ),
        Signal(
            "release",
            "Next release review",
            "decision",
            "Candidate "
            + new
            + (
                " includes schema changes."
                if diff["migration_changes"]
                else " is ahead of production."
            ),
            "Review exact-SHA evidence, migrations and workflow restart compatibility.",
        ),
        pin_signal,
    ]


def cached_release_signals(db: sqlite3.Connection, now: float) -> list[Signal]:
    row = db.execute("SELECT value FROM state WHERE key='release_cache'").fetchone()
    if row:
        with suppress(ValueError, TypeError, KeyError):
            cached = json.loads(row[0])
            if 0 <= now - cached["at"] < 900:
                return [Signal(**value) for value in cached["signals"]]
    result = release_signals()
    db.execute(
        "INSERT OR REPLACE INTO state VALUES ('release_cache', ?)",
        (json.dumps({"at": now, "signals": [asdict(s) for s in result]}),),
    )
    db.commit()
    return result


def deployment_signals() -> list[Signal]:
    if not trusted_file(DEPLOYER):
        raise RuntimeError("Installed deployer missing or unsafe")
    # Reuse its metadata-only diagnostics without reading signing key material.
    loader = SourceFileLoader("katcha_ops_deployer", str(DEPLOYER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    result = []
    labels = {
        "host_deployment_gate": "Deployment authorization",
        "signing_key_metadata": "Deployment signing permissions",
        "automatic_deploy_timer": "Deployment polling",
        "installed_tool_version": "Deployment tooling",
    }
    for check in module.deployment_control_diagnostics():
        key = check["name"]
        if key in labels:
            result.append(
                Signal(
                    key,
                    labels[key],
                    "healthy" if check["status"] == "PASS" else "warning",
                    check["detail"],
                    check.get("next_action", ""),
                )
            )
    result.append(
        Signal(
            "deployment",
            "Deployment controls",
            "healthy",
            "Installed deployment control diagnostics completed.",
        )
    )
    try:
        module.authority()
        result.append(
            Signal(
                "authority",
                "Recovery authority",
                "healthy",
                "The running API holds the current deployment authority.",
            )
        )
    except Exception:
        result.append(
            Signal(
                "authority",
                "Recovery authority",
                "unknown",
                "Current deployment authority could not be verified.",
                "Review the existing runtime fence and recovery configuration.",
            )
        )
    return result


def filesystem_signal(path: str, key: str, label: str) -> Signal:
    """Monitor capacity and inodes without traversing or deleting host data."""
    usage = shutil.disk_usage(path)
    meta = os.statvfs(path)
    if usage.total <= 0 or meta.f_blocks <= 0:
        raise ProbeError("Filesystem capacity could not be measured.")
    bytes_free = 100.0 * usage.free / usage.total
    # Some filesystems do not report inode accounting. Mark it unknown rather
    # than interpreting zero counters as a full disk.
    inodes_free = (
        100.0 * meta.f_favail / meta.f_files if meta.f_files > 0 else None
    )
    if inodes_free is not None and not 0 <= inodes_free <= 100:
        raise ProbeError("Filesystem inode measurement is invalid.")
    critical = bytes_free < 10 or (inodes_free is not None and inodes_free < 5)
    warning = bytes_free < 20 or (inodes_free is not None and inodes_free < 15)
    status = "critical" if critical else "warning" if warning else "healthy"
    detail = f"{bytes_free:.1f}% bytes free on {label}."
    detail += (
        f" {inodes_free:.1f}% inodes free."
        if inodes_free is not None else " Inode availability not reported."
    )
    action = (
        "Inspect filesystem and Docker storage growth; do not prune volumes, "
        "images, backups or logs automatically."
        if status != "healthy" else ""
    )
    return Signal(key, label, status, detail, action)


def production_container_pressure() -> Signal:
    """Read bounded Compose-container restart/OOM metadata, never environments."""
    raw = command(
        "docker", "ps", "-a",
        "--filter", "label=com.docker.compose.project=katcha-production",
        "--format", "{{.Names}}",
    )
    names = raw.splitlines()
    if not names or len(names) > 32 or len(set(names)) != len(names):
        raise ProbeError("Production container inspection inventory is unavailable.")
    if any(not re.fullmatch(r"katcha-production-[a-z0-9-]+-[0-9]+", n) for n in names):
        raise ProbeError("Production container inspection inventory is invalid.")
    # Safe, allowlisted Go-template fields. .State.Error, .Config, .LogPath
    # and arbitrary Docker inspect JSON must never reach the snapshot.
    records = command(
        "docker", "inspect", "--format",
        "{{.RestartCount}}|{{.State.OOMKilled}}|{{.State.Status}}",
        *names,
    ).splitlines()
    if len(records) != len(names):
        raise ProbeError("Production container inspection result is incomplete.")
    ooms, stopped, historical_restarts = 0, 0, 0
    for record in records:
        match = re.fullmatch(r"([0-9]{1,9})\|(true|false)\|([a-z]+)", record)
        if match is None:
            raise ProbeError("Production container inspection result is invalid.")
        restarts, oom, state = match.groups()
        historical_restarts += int(restarts)
        ooms += oom == "true"
        stopped += state not in {"running"}
    if ooms or stopped:
        return Signal(
            "containers",
            "Production container failures",
            "critical" if ooms else "warning",
            f"{ooms} containers flagged OOM-killed; {stopped} not running. "
            f"{historical_restarts} cumulative Docker restarts (not a recent rate).",
            "Inspect cgroup pressure and affected service logs before restarting containers.",
        )
    return Signal(
        "containers", "Production container failures", "healthy",
        f"{len(names)} containers running; {historical_restarts} cumulative Docker restarts.",
    )


def collect(now: float, *, release_probe: Any = None) -> list[Signal]:
    signals: list[Signal] = []

    def probe(key: str, label: str, fn: Any) -> None:
        try:
            value = fn()
            signals.extend(value if isinstance(value, list) else [value])
        except Exception as exc:
            signals.append(
                Signal(
                    key,
                    label,
                    "unknown",
                    "The check could not be completed. "
                    + (str(exc) if isinstance(exc, ProbeError) else type(exc).__name__ + "."),
                    "The remediation bot has queued diagnostics; inspect host logs.",
                )
            )

    for kind in SAFETY:
        probe(
            kind,
            "Protected backups" if kind == "backup" else "Isolated restore tests",
            lambda kind=kind: safety_signal(kind, now),
        )
    probe("temporal", "Background workflows", lambda: temporal_signal(now=now))
    probe("drift", "Release cadence", release_probe or release_signals)
    probe("deployment", "Deployment and recovery controls", deployment_signals)

    def runtime() -> Signal:
        raw = command(
            "docker",
            "ps",
            "-a",
            "--filter",
            "label=com.docker.compose.project=katcha-production",
            "--format",
            '{{.Label "com.docker.compose.service"}}|{{.State}}|{{.Status}}',
        )
        rows = [row.split("|", 2) for row in raw.splitlines()]
        from katcha.ops.production_release_observer import read_pins

        pin = read_pins(ENV)["KATCHA_RELEASE_SHA"]
        source = command(
            "git", "-C", "/opt/katcha", "show", pin + ":deploy/docker-compose.production.yml"
        )
        required = {
            "api",
            "postgres",
            "temporal",
            "renderer",
            "worker",
            "cloudflared",
            "production-worker",
            "intelligence-worker",
            "telegram-worker",
        }
        if "\n  browser:\n" in source:
            required.add("browser")
        live = {
            row[0]
            for row in rows
            if len(row) == 3
            and row[1] == "running"
            and "unhealthy" not in row[2]
            and "health: starting" not in row[2]
        }
        if not required <= live:
            return Signal(
                "runtime",
                "Production services",
                "critical",
                "One or more required services are unavailable.",
                "Inspect service health and dependencies before restarting anything.",
            )
        command(
            "curl", "--fail", "--silent", "--max-time", "8", "http://127.0.0.1:8000/v1/health/ready"
        )
        return Signal(
            "runtime",
            "Production services",
            "healthy",
            "Required containers and local API readiness responded.",
        )

    probe("runtime", "Production services", runtime)
    probe(
        "root_storage", "Root filesystem",
        lambda: filesystem_signal("/", "root_storage", "Root filesystem"),
    )
    probe(
        "storage", "Persistent storage",
        lambda: filesystem_signal("/srv/katcha", "storage", "Persistent storage"),
    )
    probe("containers", "Production container failures", production_container_pressure)

    def notification() -> Signal:
        notifier = installed_notifier()
        if notifier.configuration() is None:
            return Signal(
                "telegram",
                "Telegram alerts",
                "critical",
                "Telegram delivery is not configured.",
                "Configure the existing operator bot in the protected host environment.",
            )
        return Signal(
            "telegram",
            "Telegram alerts",
            "healthy",
            "Operator destination configured; delivery failures are tracked separately.",
        )

    probe("telegram", "Telegram alerts", notification)

    def deployment_notifications() -> Signal:
        timer = properties("katcha-production-deploy-notify.timer")
        service = properties("katcha-production-deploy-notify.service")
        enabled = timer.get("UnitFileState") == "enabled" and timer.get("ActiveState") == "active"
        if not enabled or service.get("Result") != "success":
            return Signal(
                "deployment_notifications",
                "Signed deployment request alerts",
                "warning",
                "Deployment receipt and expired-request notifications are unavailable.",
                "Inspect the existing deployment notifier service and enable its timer.",
            )
        return Signal(
            "deployment_notifications",
            "Signed deployment request alerts",
            "healthy",
            "The existing receipt and expired-request notifier is scheduled.",
        )

    probe("deployment_notifications", "Signed deployment request alerts", deployment_notifications)
    return signals


def connect(root: Path) -> sqlite3.Connection:
    root.mkdir(mode=0o755, parents=True, exist_ok=True)
    path = root / "ledger.sqlite3"
    if root.is_symlink() or path.is_symlink():
        raise ValueError("Unsafe operations storage")
    root.chmod(0o755)
    db = sqlite3.connect(path, timeout=5)
    path.chmod(0o600)
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE IF NOT EXISTS incidents (
            id INTEGER PRIMARY KEY, key TEXT NOT NULL, label TEXT NOT NULL,
            status TEXT NOT NULL, detail TEXT NOT NULL, action TEXT NOT NULL,
            opened REAL NOT NULL, updated REAL NOT NULL, resolved REAL,
            bot TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
            attempted REAL);
        CREATE UNIQUE INDEX IF NOT EXISTS one_active_incident ON incidents(key)
            WHERE resolved IS NULL;
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY, incident INTEGER NOT NULL, at REAL NOT NULL,
            message TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS release_reviews (sha TEXT PRIMARY KEY, at REAL, data TEXT);
    """)
    return db


def event(db: sqlite3.Connection, ident: int, now: float, message: str) -> None:
    db.execute("INSERT INTO events(incident, at, message) VALUES (?, ?, ?)", (ident, now, message))


def stabilize_temporal_signal(
    db: sqlite3.Connection, signal: Signal, row: sqlite3.Row | None, now: float
) -> Signal:
    """Keep an incident active while a rolling failed-run window merely goes quiet.

    Do not call an elapsed lookback a verified recovery. Require six hours of
    continuously monitored quiet before retiring an alert; interrupted monitoring
    restarts that quiet window.
    """
    if signal.key != "temporal":
        return signal
    if signal.status != "healthy" or row is None:
        db.execute("DELETE FROM state WHERE key='temporal_quiet_since'")
        return signal
    previous = db.execute("SELECT value FROM state WHERE key='checked_at'").fetchone()
    quiet = db.execute("SELECT value FROM state WHERE key='temporal_quiet_since'").fetchone()
    try:
        prior_check = float(previous[0]) if previous else 0
        since = float(quiet[0]) if quiet else now
    except (TypeError, ValueError, OverflowError):
        prior_check, since = 0, now
    if not 0 <= now - prior_check <= TEMPORAL_MAX_GAP_SECONDS or now < since:
        since = now
    db.execute(
        "INSERT OR REPLACE INTO state VALUES ('temporal_quiet_since', ?)", (str(since),)
    )
    if now - since < TEMPORAL_QUIET_SECONDS:
        return Signal(
            "temporal",
            "Background workflows",
            "warning",
            "No failed executions in the past hour; observing a six-hour quiet period. "
            "Earlier failed executions remain in Temporal history.",
            "Inspect the earlier failed executions; an expired lookback is not a successful retry.",
        )
    return signal


def track_temporal_cause_changes(
    db: sqlite3.Connection, signal: Signal, row: sqlite3.Row | None,
    now: float, *, quiet_restart: bool
) -> None:
    """Page only after a category change persists across two observations."""
    if signal.key != "temporal" or signal.status != "warning":
        return
    if not re.fullmatch(r"[0-9a-f]{24}", signal.fingerprint):
        return
    saved = db.execute(
        "SELECT value FROM state WHERE key='temporal_cause_signature'"
    ).fetchone()
    old = saved[0] if saved else None
    pending = db.execute(
        "SELECT value FROM state WHERE key='temporal_pending_signature'"
    ).fetchone()
    if old is None or row is None or quiet_restart:
        db.execute(
            "INSERT OR REPLACE INTO state VALUES ('temporal_cause_signature', ?)",
            (signal.fingerprint,),
        )
        db.execute("DELETE FROM state WHERE key='temporal_pending_signature'")
    elif signal.fingerprint == old:
        db.execute("DELETE FROM state WHERE key='temporal_pending_signature'")
    elif pending and pending[0] == signal.fingerprint:
        event(
            db, row["id"], now,
            "Background workflows: a sustained change in failure types/causes "
            "was observed. " + signal.detail + " " + signal.action,
        )
        db.execute(
            "INSERT OR REPLACE INTO state VALUES ('temporal_cause_signature', ?)",
            (signal.fingerprint,),
        )
        db.execute("DELETE FROM state WHERE key='temporal_pending_signature'")
    else:
        db.execute(
            "INSERT OR REPLACE INTO state VALUES ('temporal_pending_signature', ?)",
            (signal.fingerprint,),
        )


def reconcile(db: sqlite3.Connection, signals: list[Signal], now: float) -> None:
    observed: list[Signal] = []
    for signal in signals:
        row = db.execute(
            "SELECT * FROM incidents WHERE key=? AND resolved IS NULL", (signal.key,)
        ).fetchone()
        new_failure_after_quiet = (
            signal.key == "temporal"
            and signal.status == "warning"
            and row is not None
            and db.execute(
                "SELECT 1 FROM state WHERE key='temporal_quiet_since'"
            ).fetchone() is not None
        )
        track_temporal_cause_changes(
            db, signal, row, now, quiet_restart=new_failure_after_quiet
        )
        signal = stabilize_temporal_signal(db, signal, row, now)
        observed.append(signal)
        if new_failure_after_quiet:
            event(
                db, row["id"], now,
                "Background workflows: new failures detected after a quiet period. "
                + signal.detail + " " + signal.action,
            )
        if signal.status == "healthy":
            if row:
                if (
                    row["attempted"]
                    and signal.key in SAFETY
                    and (signal.completed_at is None or signal.completed_at <= row["attempted"])
                ):
                    continue
                db.execute(
                    "UPDATE incidents SET resolved=?, updated=?, bot=? WHERE id=?",
                    (now, now, "quieted" if signal.key == "temporal" else "verified", row["id"]),
                )
                message = (
                    "Background workflows: six hours without observed failures; "
                    "previously failed executions are retained for investigation."
                    if signal.key == "temporal"
                    else signal.label + ": recovery verified by a fresh check."
                )
                event(db, row["id"], now, message)
                if signal.key == "temporal":
                    db.execute("DELETE FROM state WHERE key='temporal_cause_signature'")
                    db.execute("DELETE FROM state WHERE key='temporal_pending_signature'")
            continue
        if row is None:
            ident = db.execute(
                "INSERT INTO incidents(key,label,status,detail,action,opened,updated) "
                "VALUES (?,?,?,?,?,?,?)",
                (signal.key, signal.label, signal.status, signal.detail, signal.action, now, now),
            ).lastrowid
            event(db, ident, now, signal.label + ": " + signal.detail + " " + signal.action)
        else:
            db.execute(
                "UPDATE incidents SET status=?,detail=?,action=?,updated=? WHERE id=?",
                (signal.status, signal.detail, signal.action, now, row["id"]),
            )
            if row["status"] != signal.status:
                event(
                    db, row["id"], now, signal.label + ": " + signal.status + ". " + signal.detail
                )
            if signal.key == "release" and row["detail"] != signal.detail:
                db.execute(
                    "UPDATE incidents SET bot='queued',attempts=0,attempted=NULL WHERE id=?",
                    (row["id"],),
                )
            elif signal.key == "release" and row["attempted"] and now - row["attempted"] >= 3600:
                db.execute("UPDATE incidents SET bot='queued' WHERE id=?", (row["id"],))
    db.execute(
        "INSERT OR REPLACE INTO state VALUES ('signals', ?)",
        (json.dumps([asdict(s) for s in observed]),),
    )
    db.execute("INSERT OR REPLACE INTO state VALUES ('checked_at', ?)", (str(now),))
    db.commit()


def remediate(db: sqlite3.Connection, now: float) -> None:
    """One durable attempt per incident; no arbitrary commands or inferred authority."""
    rows = db.execute("SELECT * FROM incidents WHERE resolved IS NULL AND bot='queued'").fetchall()
    safety_claimed = (
        db.execute(
            "SELECT 1 FROM incidents WHERE resolved IS NULL AND bot='verifying' "
            "AND key IN ('backup','restore') LIMIT 1"
        ).fetchone()
        is not None
    )
    for row in rows:
        key = row["key"]
        if key not in SAFETY:
            if key == "release":
                match = re.search(r"\b[0-9a-f]{40}\b", row["detail"])
                if match and trusted_file(DEPLOYER):
                    # Only the read-only diagnostic mode is callable by this bot.
                    try:
                        db.execute(
                            "UPDATE incidents SET attempted=?,attempts=attempts+1 WHERE id=?",
                            (now, row["id"]),
                        )
                        db.commit()
                        raw = command(
                            str(DEPLOYER),
                            "--diagnose-release",
                            match[0],
                            timeout=120,
                            accept_failure=True,
                        )
                        diagnosis = json.loads(raw)
                        if diagnosis.get("target_sha") != match[0]:
                            raise ValueError("Wrong release evidence")
                        counts: dict[str, int] = {}
                        for check in diagnosis["checks"]:
                            status = check.get("status")
                            if status in {"PASS", "BLOCKED", "UNKNOWN", "REVIEW_REQUIRED"}:
                                counts[status] = counts.get(status, 0) + 1
                        # Preserve reviewable evidence, excluding provider text and raw output.
                        names = {
                            "target_sha",
                            "host_identity",
                            "host_deployment_gate",
                            "signing_key_metadata",
                            "automatic_deploy_timer",
                            "installed_tool_version",
                            "production_readiness",
                            "release_plan",
                            "backup_restore_review",
                            "github_request_approval",
                            "migration_review",
                            "temporal_workflow_review",
                        }
                        review = {
                            "target_sha": match[0],
                            "checked_at": now,
                            "checks": [
                                {"name": c["name"], "status": c["status"]}
                                for c in diagnosis["checks"]
                                if c.get("name") in names and c.get("status") in counts
                            ],
                            "deployment_authorized": False,
                        }
                        plan = diagnosis.get("release_plan", {})
                        count = plan.get("temporal_running_count")
                        if type(count) is int and 0 <= count <= 100:
                            review["running_workflows"] = count
                        migrations = plan.get("migration_files", [])
                        if isinstance(migrations, list):
                            review["migrations"] = [
                                p
                                for p in migrations
                                if isinstance(p, str)
                                and re.fullmatch(r"migrations/versions/[0-9]{4}_[a-z0-9_]+\.py", p)
                            ]
                        previous = db.execute(
                            "SELECT data FROM release_reviews WHERE sha=?", (match[0],)
                        ).fetchone()
                        old_review = json.loads(previous[0]) if previous else {}
                        unchanged = {k: v for k, v in old_review.items() if k != "checked_at"} == {
                            k: v for k, v in review.items() if k != "checked_at"
                        }
                        db.execute(
                            "INSERT OR REPLACE INTO release_reviews VALUES (?,?,?)",
                            (match[0], now, json.dumps(review)),
                        )
                        if not unchanged:
                            event(
                                db,
                                row["id"],
                                now,
                                "Exact release "
                                + match[0]
                                + ": diagnostic review prepared "
                                + json.dumps(counts, sort_keys=True)
                                + ". Approval remains required in the manual deployment runbook.",
                            )
                    except Exception:
                        if not row["attempts"]:
                            event(
                                db,
                                row["id"],
                                now,
                                "Exact release diagnostic unavailable; "
                                "review installed tooling and GitHub connectivity.",
                            )
            db.execute("UPDATE incidents SET bot='operator_decision' WHERE id=?", (row["id"],))
            if not row["attempts"]:
                event(
                    db, row["id"], now, row["label"] + ": diagnostics collected; " + row["action"]
                )
            continue
        if row["attempts"]:
            continue
        if safety_claimed:
            continue
        try:
            # Serialize with the deployer. Never race its final backup/restore checks.
            fd = os.open(DEPLOY_LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                states = [properties(unit) for unit, _, _ in SAFETY.values()]
                if any(s.get("ActiveState") not in {"inactive", "failed"} for s in states):
                    continue
                timer = properties(SAFETY[key][0].replace(".service", ".timer"))
                if timer.get("UnitFileState") != "enabled" or timer.get("ActiveState") != "active":
                    db.execute(
                        "UPDATE incidents SET bot='operator_decision' WHERE id=?", (row["id"],)
                    )
                    event(db, row["id"], now, row["label"] + ": restore the safety timer first.")
                    continue
                # A retry needs independently observed runtime, storage and authority.
                signals = json.loads(
                    db.execute("SELECT value FROM state WHERE key='signals'").fetchone()[0]
                )
                healthy = {s["key"] for s in signals if s["status"] == "healthy"}
                if not {"runtime", "storage", "authority"} <= healthy:
                    continue
                recent = db.execute("SELECT MAX(attempted) FROM incidents WHERE key=?", (key,))
                last = recent.fetchone()[0]
                if last is not None and now - last < 3600:
                    continue
                # Claim and commit before side effect: crash/unknown start is never replayed.
                db.execute(
                    "UPDATE incidents SET bot='verifying',attempts=1,attempted=? WHERE id=?",
                    (now, row["id"]),
                )
                event(db, row["id"], now, row["label"] + ": bot requested one safety job retry.")
                db.commit()
                safety_claimed = True
                command("systemctl", "start", "--no-block", SAFETY[key][0])
        except BlockingIOError:
            continue
        except Exception:
            db.execute("UPDATE incidents SET bot='operator_decision' WHERE id=?", (row["id"],))
            event(
                db, row["id"], now, row["label"] + ": retry outcome uncertain; inspect safety job."
            )
    # Starting a unit is not remediation success. Require a later fresh healthy signal.
    for row in db.execute("SELECT * FROM incidents WHERE resolved IS NULL AND bot='verifying'"):
        finished_without_proof = False
        if now - row["attempted"] > 60:
            with suppress(Exception):
                finished_without_proof = properties(SAFETY[row["key"]][0]).get("ActiveState") in {
                    "inactive",
                    "failed",
                }
        if finished_without_proof or now - row["attempted"] > (
            3000 if row["key"] == "backup" else 6000
        ):
            db.execute("UPDATE incidents SET bot='operator_decision' WHERE id=?", (row["id"],))
            event(
                db,
                row["id"],
                now,
                row["label"] + ": retry did not verify recovery; review required.",
            )
    db.commit()


def notify(db: sqlite3.Connection, now: float) -> None:
    rows = db.execute("SELECT * FROM events WHERE delivered=0 ORDER BY id LIMIT 10").fetchall()
    if not rows:
        return
    try:
        notifier = installed_notifier()
        credentials = notifier.configuration()
        if credentials is None:
            raise RuntimeError("Alerts not configured")
        message = "Katcha operations\n"
        included = []
        for row in rows:
            line = "Event " + str(row["id"]) + ": " + row["message"] + "\n"
            if len(message) + len(line) > 3500:
                break
            message += line
            included.append(row["id"])
        notifier.telegram_send(credentials, message + "\n" + HEALTH_URL)
        db.executemany("UPDATE events SET delivered=1 WHERE id=?", [(i,) for i in included])
        db.commit()
        db.execute("INSERT OR REPLACE INTO state VALUES ('delivery', 'healthy')")
    except Exception:
        # At-least-once delivery; a crash after Telegram acceptance can duplicate an event.
        db.execute("INSERT OR REPLACE INTO state VALUES ('delivery', 'failed')")
    db.commit()


def snapshot(db: sqlite3.Connection, root: Path) -> dict[str, Any]:
    state = dict(db.execute("SELECT key,value FROM state"))
    signals = json.loads(state.get("signals", "[]"))
    incidents = [
        dict(row)
        for row in db.execute(
            "SELECT * FROM incidents ORDER BY (resolved IS NULL) DESC, updated DESC LIMIT 100"
        )
    ]
    history = [dict(row) for row in db.execute("SELECT * FROM events ORDER BY id DESC LIMIT 100")]
    pending = db.execute("SELECT count(*) FROM events WHERE delivered=0").fetchone()[0]
    review_row = db.execute("SELECT data FROM release_reviews ORDER BY at DESC LIMIT 1").fetchone()
    payload = {
        "version": 1,
        "checked_at": float(state.get("checked_at", "0")),
        "signals": signals,
        "incidents": incidents,
        "history": history,
        "notifications": {"pending": pending, "status": state.get("delivery", "unverified")},
        "deployment_authorized": False,
        "release_review": json.loads(review_row[0]) if review_row else None,
    }
    public = root / "public"
    public.mkdir(mode=0o755, exist_ok=True)
    if public.is_symlink():
        raise ValueError("Unsafe snapshot directory")
    public.chmod(0o755)
    fd, temporary = tempfile.mkstemp(dir=public, prefix=".health-")
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(payload, out, sort_keys=True)
            out.flush()
            os.fsync(out.fileno())
            os.fchmod(out.fileno(), 0o644)
        os.replace(temporary, public / "health.json")
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return payload


def main() -> int:
    global DEADLINE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observe-only", action="store_true", help="Skip safety job retries")
    args = parser.parse_args()
    if os.geteuid() != 0 or os.uname().nodename != HOST:
        print("OPERATIONS_BLOCKED: requires root on the verified primary")
        return 2
    os.umask(0o077)
    try:
        ROOT.mkdir(mode=0o755, exist_ok=True)
        if ROOT.is_symlink() or ROOT.stat().st_uid != 0 or ROOT.stat().st_mode & 0o022:
            raise ValueError("Unsafe operations directory")
        fd = os.open(ROOT / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with connect(ROOT) as db:
                now = time.time()
                if db.execute("SELECT value FROM state WHERE key='checked_at'").fetchone() is None:
                    event(
                        db,
                        0,
                        now,
                        "Operations monitoring is active. Review health and "
                        "outstanding decisions in the Operations workspace.",
                    )
                DEADLINE = time.monotonic() + 120
                reconcile(
                    db, collect(now, release_probe=lambda: cached_release_signals(db, now)), now
                )
                snapshot(db, ROOT)
                DEADLINE = time.monotonic() + 45
                if not args.observe_only:
                    remediate(db, now)
                notify(db, now)
                report = snapshot(db, ROOT)
        print(
            json.dumps(
                {
                    "status": "OBSERVED",
                    "checked_at": report["checked_at"],
                    "pending_notifications": report["notifications"]["pending"],
                }
            )
        )
        return 0
    except BlockingIOError:
        print("OPERATIONS_BUSY: another observation is running")
        return 0
    except Exception:
        print("OPERATIONS_FAILED: inspect protected host diagnostics; previous snapshot preserved")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
