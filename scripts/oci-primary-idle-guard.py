#!/usr/bin/env python3
"""Reversible, low-CPU OCI A1 memory safeguard; requires verified Bastion access.

The guard is an emergency mitigation of OCI Always Free idle reclamation risk,
NOT a guarantee, production workload, Oracle policy exemption, or proof of safety.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import time

HOST_SCRIPT = Path("/usr/local/libexec/katcha-idle-guard.py")
HOST_UNIT = Path("/etc/systemd/system/katcha-idle-guard.service")
MIB = 1024 * 1024
RESERVE_MIB = 1408
MIN_AVAILABLE_BEFORE_MIB = 6144

DAEMON_SOURCE = """#!/usr/bin/env python3
\"\"\"Bounded anonymous RAM reserve, suspended immediately under memory pressure.\"\"\"
import mmap
from pathlib import Path
import signal
import time

MIB = 1024 * 1024
TARGET_MIB = 1408
CHUNK_MIB = 64
START_AVAILABLE_MIB = 6144
STOP_AVAILABLE_MIB = 4096
running = True


def stop(*_args):
    global running
    running = False


def available_mib():
    fields = {}
    for line in Path('/proc/meminfo').read_text().splitlines():
        key, _, value = line.partition(':')
        if key == 'MemAvailable':
            return int(value.strip().split()[0]) // 1024
    raise RuntimeError('MemAvailable not found')


def run():
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    regions = []
    try:
        while running:
            if available_mib() < STOP_AVAILABLE_MIB and regions:
                regions.clear()
                print('SAFEGUARD_RELEASED_MEMORY_PRESSURE', flush=True)
            if not regions and available_mib() >= START_AVAILABLE_MIB:
                print('SAFEGUARD_RESERVING_MIB=' + str(TARGET_MIB), flush=True)
                try:
                    for _ in range(TARGET_MIB // CHUNK_MIB):
                        if not running or available_mib() < STOP_AVAILABLE_MIB + CHUNK_MIB:
                            break
                        region = mmap.mmap(-1, CHUNK_MIB * MIB)
                        for offset in range(0, CHUNK_MIB * MIB, 4096):
                            region[offset] = 1
                        regions.append(region)
                    if len(regions) * CHUNK_MIB != TARGET_MIB:
                        regions.clear()
                        print('SAFEGUARD_RESERVATION_INCOMPLETE', flush=True)
                except (MemoryError, OSError):
                    regions.clear()
                    print('SAFEGUARD_ALLOC_FAILED', flush=True)
            for _ in range(30):
                if not running:
                    break
                time.sleep(1)
    finally:
        regions.clear()


if __name__ == '__main__':
    run()
"""

UNIT_SOURCE = f"""[Unit]
Description=Temporary Katcha A1 idle-reclamation memory safeguard
After=katcha.service
RequiresMountsFor=/srv/katcha

[Service]
Type=simple
User=nobody
Group=nogroup
ExecStart=/usr/bin/python3 {HOST_SCRIPT}
Restart=always
RestartSec=30
MemoryAccounting=yes
MemoryMax=1800M
MemorySwapMax=0
CPUQuota=10%
OOMScoreAdjust=900
NoNewPrivileges=yes
PrivateNetwork=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
TasksMax=24

[Install]
WantedBy=multi-user.target
"""


def meminfo() -> tuple[int, int]:
    fields = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, _, value = line.partition(":")
        if key in ("MemTotal", "MemAvailable"):
            fields[key] = int(value.strip().split()[0]) // 1024
    if set(fields) != {"MemTotal", "MemAvailable"}:
        raise RuntimeError("unable to read system memory counters")
    return fields["MemTotal"], fields["MemAvailable"]


def command(*argv: str, check: bool = True) -> str:
    result = subprocess.run(argv, text=True, capture_output=True, check=False, timeout=45)
    if check and result.returncode != 0:
        # Only print the failed command category, not arbitrary stderr.
        raise RuntimeError("host command failed: " + argv[0] + " " + argv[1])
    return result.stdout.strip()


def host_identity() -> None:
    if os.geteuid() != 0:
        raise RuntimeError("expected sudo root on verified production SSH target")
    if not Path("/etc/katcha/katcha.env").is_file():
        raise RuntimeError("Katcha production configuration not present")
    command("mountpoint", "-q", "/srv/katcha")
    command("systemctl", "is-active", "--quiet", "katcha.service")
    total, available = meminfo()
    if not 10240 <= total <= 20480:
        raise RuntimeError("host is not the expected 12 GB A1 memory configuration")
    if available < MIN_AVAILABLE_BEFORE_MIB:
        raise RuntimeError("insufficient MemAvailable; refused to reserve memory")


def write_exact_file(path: Path, expected: str, mode: int) -> bool:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise RuntimeError("unexpected guard destination type")
    if path.exists():
        if path.read_text() != expected:
            raise RuntimeError("existing guard file differs; no overwrite permitted")
        return False
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".new")
    if temp.exists() or temp.is_symlink():
        raise RuntimeError("unexpected installation staging file")
    try:
        with temp.open("x", encoding="utf-8") as stream:
            stream.write(expected)
        temp.chmod(mode)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    return True


def show_host_status() -> None:
    total, available = meminfo()
    print("host_memory_total_mib:", total)
    print("host_memory_available_mib:", available)
    print("katcha_service:", command("systemctl", "is-active", "katcha.service", check=False))
    print("safeguard_service:", command("systemctl", "is-active",
                                        "katcha-idle-guard.service", check=False))
    current = command("systemctl", "show", "-p", "MemoryCurrent", "--value",
                      "katcha-idle-guard.service", check=False)
    if current.isdecimal():
        print("safeguard_memory_current_mib:", int(current) // MIB)
    else:
        print("safeguard_memory_current_mib: UNAVAILABLE")


def install_on_host() -> None:
    host_identity()
    if HOST_SCRIPT.exists() != HOST_UNIT.exists():
        raise RuntimeError("partial guard installation detected; refusing modification")
    if HOST_SCRIPT.exists() and (
        HOST_SCRIPT.read_text() != DAEMON_SOURCE or HOST_UNIT.read_text() != UNIT_SOURCE
    ):
        raise RuntimeError("installed safeguard differs from reviewed version; no overwrite")
    print("GUARD_PREFLIGHT_OK: production active, durable mount present, memory headroom safe")
    write_exact_file(HOST_SCRIPT, DAEMON_SOURCE, 0o644)
    write_exact_file(HOST_UNIT, UNIT_SOURCE, 0o644)
    command("systemctl", "daemon-reload")
    command("systemctl", "enable", "--now", "katcha-idle-guard.service")
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        state = command("systemctl", "is-active", "katcha-idle-guard.service", check=False)
        measured = command("systemctl", "show", "-p", "MemoryCurrent", "--value",
                           "katcha-idle-guard.service", check=False)
        if state == "active" and measured.isdecimal() and int(measured) > 1200 * MIB:
            command("systemctl", "is-active", "--quiet", "katcha.service")
            print("GUARD_ACTIVE: verified >1200 MiB resident; 1800M hard cgroup limit")
            show_host_status()
            return
        time.sleep(2)
    command("systemctl", "disable", "--now", "katcha-idle-guard.service", check=False)
    raise RuntimeError("guard failed to establish memory reserve; service disabled")


def remote_main(action: str) -> int:
    try:
        if action == "install":
            install_on_host()
        elif action == "status":
            show_host_status()
        elif action == "disable":
            if os.geteuid() != 0:
                raise RuntimeError("sudo root required")
            command("systemctl", "disable", "--now", "katcha-idle-guard.service")
            print("GUARD_DISABLED: no Katcha application services changed")
        else:
            raise RuntimeError("unknown remote action")
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print("GUARD_STOP: " + str(exc), file=sys.stderr)
        return 2
    return 0


def operator_main(action: str) -> int:
    """Use only an existing verified private Bastion tunnel and pinned SSH key."""
    home = Path.home()
    key = home / ".ssh/katcha-oci"
    known = home / ".config/katcha/production/bastion/target_known_hosts"
    if not key.is_file() or not known.is_file():
        print("GUARD_STOP: pinned production key/hostkeys missing", file=sys.stderr)
        return 2
    try:
        result = subprocess.run(
            ["ssh", "-F", "/dev/null", "-4", "-i", str(key),
             "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
             "-o", "StrictHostKeyChecking=yes",
             "-o", "UserKnownHostsFile=" + str(known),
             "-o", "ConnectTimeout=10", "-p", "22022", "ubuntu@127.0.0.1",
             "sudo", "-n", "python3", "-", "--remote", action],
            input=Path(__file__).read_text(encoding="utf-8"),
            text=True, capture_output=True, check=False, timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        print("GUARD_STOP: verified production SSH unavailable or timed out", file=sys.stderr)
        return 2
    # Print only explicit known-safe report lines. Remote command stderr may
    # contain sensitive paths/environment, so never relay it unfiltered.
    for line in result.stdout.splitlines():
        if line.startswith(("GUARD_", "host_memory_", "katcha_service:",
                            "safeguard_service:", "safeguard_memory_")):
            print(line)
    if result.returncode:
        print("GUARD_STOP: remote action failed; inspect host logs privately "
              "(no production change should be assumed successful)", file=sys.stderr)
        return 2
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "status", "disable"))
    parser.add_argument("--remote", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    return remote_main(args.action) if args.remote else operator_main(args.action)


if __name__ == "__main__":
    raise SystemExit(main())
