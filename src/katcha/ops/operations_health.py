"""Persistent host operations monitor and bounded remediation bot (stdlib only).

The ledger is private. Only allowlisted summaries reach the API or Telegram.
No release approval, production restart, migration or infrastructure write lives here.
"""

from __future__ import annotations

import argparse
import fcntl
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


def temporal_signal() -> Signal:
    """Normal active work is healthy; repeated activity failures need attention."""
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
    failed = json.loads(
        command(
            *prefix,
            "list",
            *common,
            "--query",
            'ExecutionStatus="Failed" AND CloseTime > "'
            + datetime.fromtimestamp(time.time() - 3600, UTC).isoformat()
            + '"',
            "--limit",
            "101",
        )
    )
    if not isinstance(failed, list):
        raise ValueError("Failed workflow inventory incomplete")
    rows = json.loads(
        command(*prefix, "list", *common, "--query", 'ExecutionStatus="Running"', "--limit", "101")
    )
    if not isinstance(rows, list) or len(rows) > 100:
        raise ValueError("Workflow inventory incomplete")
    pending = 0
    failing = 0
    for row in rows:
        execution = row["execution"]
        wid, rid = execution["workflowId"], execution["runId"]
        if not isinstance(wid, str) or not isinstance(rid, str) or len(wid) > 255:
            raise ValueError("Workflow identity invalid")
        data = json.loads(
            command(*prefix, "describe", *common, "--workflow-id", wid, "--run-id", rid)
        )
        if data["workflowExecutionInfo"]["execution"] != execution:
            raise ValueError("Workflow identity changed")
        activities = data.get("pendingActivities") or []
        pending += len(activities)
        failing += sum(
            bool(a.get("lastFailure")) and int(a.get("attempt", 0)) >= 3 for a in activities
        )
    if failing or failed:
        return Signal(
            "temporal",
            "Background workflows",
            "warning",
            f"{len(failed)} recently failed workflows; "
            f"{failing} activities have repeated failures.",
            "Inspect workflow history and repair the failing dependency.",
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
    new = observer.current_main(release_eligibility.api_get)
    if old == new:
        return [
            Signal("drift", "Release cadence", "healthy", "Production matches main."),
            Signal(
                "release", "Next release review", "healthy", "No newer main release needs review."
            ),
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
    ]


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


def collect(now: float) -> list[Signal]:
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
    probe("temporal", "Background workflows", temporal_signal)
    probe("drift", "Release cadence", release_signals)
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
    disk = shutil.disk_usage("/srv/katcha")
    free = 100 * disk.free / disk.total
    signals.append(
        Signal(
            "storage",
            "Persistent storage",
            "critical" if free < 10 else "warning" if free < 20 else "healthy",
            f"{free:.1f}% free on the production data volume.",
            "Review storage growth; preserve backups and application data." if free < 20 else "",
        )
    )

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


def reconcile(db: sqlite3.Connection, signals: list[Signal], now: float) -> None:
    for signal in signals:
        row = db.execute(
            "SELECT * FROM incidents WHERE key=? AND resolved IS NULL", (signal.key,)
        ).fetchone()
        if signal.status == "healthy":
            if row:
                if (
                    row["attempted"]
                    and signal.key in SAFETY
                    and (signal.completed_at is None or signal.completed_at <= row["attempted"])
                ):
                    continue
                db.execute(
                    "UPDATE incidents SET resolved=?, updated=?, bot='verified' WHERE id=?",
                    (now, now, row["id"]),
                )
                event(db, row["id"], now, signal.label + ": recovery verified by a fresh check.")
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
                db.execute("UPDATE incidents SET bot='queued',attempts=0 WHERE id=?", (row["id"],))
    db.execute(
        "INSERT OR REPLACE INTO state VALUES ('signals', ?)",
        (json.dumps([asdict(s) for s in signals]),),
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
                        db.execute(
                            "INSERT OR REPLACE INTO release_reviews VALUES (?,?,?)",
                            (match[0], now, json.dumps(review)),
                        )
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
                        event(
                            db,
                            row["id"],
                            now,
                            "Exact release diagnostic unavailable; "
                            "review installed tooling and GitHub connectivity.",
                        )
            db.execute("UPDATE incidents SET bot='operator_decision' WHERE id=?", (row["id"],))
            event(db, row["id"], now, row["label"] + ": diagnostics collected; " + row["action"])
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
                reconcile(db, collect(now), now)
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
