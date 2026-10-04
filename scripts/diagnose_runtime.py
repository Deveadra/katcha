"""Read-only resource snapshot. Container environment and logs are never exported."""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
import urllib.request
from pathlib import Path

# Aggregate evidence only: never export SQL text, connection addresses or users.
# The transaction is read-only and bounded independently of Docker's timeout.
POSTGRES_SNAPSHOT_SQL = """
BEGIN READ ONLY;
SET LOCAL statement_timeout = '3s';
SELECT json_build_object(
    'max_connections', current_setting('max_connections')::int,
    'connections', (SELECT count(*) FROM pg_stat_activity),
    'active_connections', (SELECT count(*) FROM pg_stat_activity
        WHERE state = 'active' AND pid <> pg_backend_pid()),
    'idle_in_transaction', (SELECT count(*) FROM pg_stat_activity
        WHERE state LIKE 'idle in transaction%'),
    'lock_waiters', (SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock'),
    'blocked_connections', (SELECT count(*) FROM pg_stat_activity
        WHERE cardinality(pg_blocking_pids(pid)) > 0),
    'long_transactions', (SELECT count(*) FROM pg_stat_activity
        WHERE xact_start IS NOT NULL
          AND pid <> pg_backend_pid()
          AND xact_start < clock_timestamp() - interval '30 seconds'),
    'oldest_transaction_seconds', COALESCE((
        SELECT max(extract(epoch FROM (clock_timestamp() - xact_start)))
        FROM pg_stat_activity
        WHERE xact_start IS NOT NULL AND pid <> pg_backend_pid()
    ), 0)
);
ROLLBACK;
"""


def docker(*args: str) -> str:
    return subprocess.run(
        ["docker", *args], check=True, capture_output=True, text=True, timeout=10
    ).stdout


def _pressure_snapshot(path: Path) -> dict:
    """Parse Linux PSI counters without exporting process or workload details."""
    if not path.exists():
        return {}
    result = {}
    try:
        for line in path.read_text().splitlines():
            parts = line.split()
            if not parts:
                continue
            values = {}
            for item in parts[1:]:
                if "=" not in item:
                    continue
                key, value = item.split("=", 1)
                values[key] = int(value) if key == "total" else float(value)
            result[parts[0]] = values
    except (OSError, ValueError):
        return {}
    return result


def _integer_file_snapshot(path: Path) -> dict:
    if not path.exists():
        return {}
    result = {}
    try:
        for line in path.read_text().splitlines():
            key, value = line.split(None, 1)
            result[key] = int(value)
    except (OSError, ValueError):
        return {}
    return result


def docker_event_snapshot(until: str) -> list[dict]:
    """Return bounded container lifecycle evidence with only allow-listed attributes."""
    output = docker(
        "events",
        "--since",
        "24h",
        "--until",
        until,
        "--filter",
        "label=com.docker.compose.project=katcha",
        "--format",
        "{{json .}}",
    )
    events = []
    for line in output.splitlines()[-200:]:
        try:
            row = json.loads(line)
        except (TypeError, ValueError):
            continue
        attributes = row.get("Actor", {}).get("Attributes", {}) or {}
        events.append(
            {
                "time": row.get("time"),
                "time_nano": row.get("timeNano"),
                "action": row.get("Action") or row.get("status"),
                "service": attributes.get("com.docker.compose.service"),
                "exit_code": attributes.get("exitCode"),
                "signal": attributes.get("signal"),
            }
        )
    return events


def postgres_snapshot(containers: list[dict]) -> dict:
    postgres = next(
        (
            row
            for row in containers
            if row.get("Config", {}).get("Labels", {}).get("com.docker.compose.service")
            == "postgres"
        ),
        None,
    )
    if postgres is None or postgres.get("State", {}).get("Status") != "running":
        return {"available": False, "hint": "PostgreSQL container is not running."}
    try:
        output = docker(
            "exec",
            postgres["Id"],
            "psql",
            "-X",
            "-qAt",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "katcha",
            "-d",
            "katcha",
            "-c",
            POSTGRES_SNAPSHOT_SQL,
        )
        return {"available": True, **json.loads(output)}
    except Exception:
        return {
            "available": False,
            "hint": "PostgreSQL read-only probe failed or timed out; inspect database health.",
        }


def collect() -> dict:
    captured_at = dt.datetime.now(dt.UTC).isoformat()
    disk = shutil.disk_usage(Path.cwd())
    snapshot = {
        "captured_at": captured_at,
        "host": {
            "cpu_count": os.cpu_count(),
            "disk_total_bytes": disk.total,
            "disk_used_bytes": disk.used,
            "disk_free_bytes": disk.free,
        },
    }
    if hasattr(os, "getloadavg"):
        snapshot["host"]["load_average"] = os.getloadavg()
    memory = Path("/proc/meminfo")
    if memory.exists():
        snapshot["host"]["memory_kib"] = {
            key: int(value.split()[0])
            for line in memory.read_text().splitlines()
            for key, value in [line.split(":", 1)]
            if key in {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"}
        }
    pressure = {
        name: values
        for name in ("cpu", "memory", "io")
        if (values := _pressure_snapshot(Path("/proc/pressure") / name))
    }
    if pressure:
        snapshot["host"]["pressure"] = pressure
    memory_events = _integer_file_snapshot(Path("/sys/fs/cgroup/memory.events"))
    if memory_events:
        snapshot["host"]["cgroup_memory_events"] = memory_events
    try:
        with urllib.request.urlopen("http://127.0.0.1:8765/runtime/status", timeout=3) as response:
            runtime = json.load(response)
        snapshot["launcher"] = {
            key: runtime.get(key)
            for key in ("workspace_ready", "desired_running", "phase", "stage", "services")
        }
    except Exception:
        snapshot["launcher"] = {"available": False}
    try:
        ids = docker("ps", "-aq", "--filter", "label=com.docker.compose.project=katcha").split()
        containers = json.loads(docker("inspect", *ids)) if ids else []
        snapshot["containers"] = [
            {
                "service": row.get("Config", {})
                .get("Labels", {})
                .get("com.docker.compose.service"),
                "state": {
                    key: row.get("State", {}).get(key)
                    for key in ("Status", "OOMKilled", "ExitCode", "StartedAt", "FinishedAt")
                },
                "health": row.get("State", {}).get("Health", {}).get("Status"),
                "health_failing_streak": row.get("State", {})
                .get("Health", {})
                .get("FailingStreak"),
                "restart_count": row.get("RestartCount"),
                "memory_limit_bytes": row.get("HostConfig", {}).get("Memory"),
                "cpu_limit_nano": row.get("HostConfig", {}).get("NanoCpus"),
            }
            for row in containers
        ]
        snapshot["resource_usage"] = (
            [
                json.loads(line)
                for line in docker(
                    "stats", "--no-stream", "--format", "{{json .}}", *ids
                ).splitlines()
            ]
            if ids
            else []
        )
        snapshot["postgres"] = postgres_snapshot(containers)
        try:
            snapshot["docker_events_24h"] = docker_event_snapshot(captured_at)
        except Exception:
            snapshot["docker_events_24h"] = {
                "available": False,
                "hint": "Docker lifecycle event history was unavailable or timed out.",
            }
    except Exception:
        snapshot["docker"] = {
            "available": False,
            "hint": "Docker missing, unavailable, or query exceeded 10 seconds.",
        }
    return snapshot


if __name__ == "__main__":
    print(json.dumps(collect(), indent=2))
