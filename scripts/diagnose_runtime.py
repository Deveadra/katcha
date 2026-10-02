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
        WHERE cardinality(pg_blocking_pids(pid)) > 0)
);
ROLLBACK;
"""


def docker(*args: str) -> str:
    return subprocess.run(
        ["docker", *args], check=True, capture_output=True, text=True, timeout=10
    ).stdout


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
    snapshot = {
        "captured_at": dt.datetime.now(dt.UTC).isoformat(),
        "host": {
            "cpu_count": os.cpu_count(),
            "disk_free_bytes": shutil.disk_usage(Path.cwd()).free,
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
    except Exception:
        snapshot["docker"] = {
            "available": False,
            "hint": "Docker missing, unavailable, or query exceeded 10 seconds.",
        }
    return snapshot


if __name__ == "__main__":
    print(json.dumps(collect(), indent=2))
