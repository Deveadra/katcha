"""Read-only resource snapshot. Container environment and logs are never exported."""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
import urllib.request
from pathlib import Path


def docker(*args: str) -> str:
    return subprocess.run(
        ["docker", *args], check=True, capture_output=True, text=True, timeout=10
    ).stdout


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
    except Exception:
        snapshot["docker"] = {
            "available": False,
            "hint": "Docker missing, unavailable, or query exceeded 10 seconds.",
        }
    return snapshot


if __name__ == "__main__":
    print(json.dumps(collect(), indent=2))
