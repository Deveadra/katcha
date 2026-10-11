#!/usr/bin/env python3
"""Manage only an existing OCI Bastion access path; no compute or disk creation."""
from __future__ import annotations

import argparse
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import time
from contextlib import contextmanager

HOME = Path.home()
REPO = "Deveadra/katcha"
REGION = "us-ashburn-1"
PORT = 22022
OCI = HOME / ".cache/katcha/oci-cli-3.94.1/bin/oci"
ADMIN_CONFIG = HOME / ".oci/config"
API_CONFIG = HOME / ".config/katcha/oci-github-recovery/config"
BASE = HOME / ".config/katcha/production/bastion"
STATE_DIR = BASE / "operator-tunnel"
TARGET_HOSTKEYS = BASE / "target_known_hosts"
TARGET_KEY = HOME / ".ssh/katcha-oci"
SCRIPTS = Path(__file__).resolve().parent
SWITCHES = ("KATCHA_OCI_RECOVERY_CONFIGURED", "KATCHA_EXTERNAL_COMPUTE_ENABLED",
            "KATCHA_OCI_PAID_FALLBACK_ENABLED")


class AccessError(Exception):
    pass


def run(args: list[str], timeout: int = 45) -> str:
    try:
        result = subprocess.run(args, capture_output=True, text=True, check=False, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AccessError("local command unavailable or timed out: " + Path(args[0]).name) from exc
    if result.returncode:
        raise AccessError("command failed: " + Path(args[0]).name
                          + " (exit " + str(result.returncode) + ")")
    return result.stdout.strip()


def oci(*args: str) -> str:
    if not OCI.is_file() or not API_CONFIG.is_file():
        raise AccessError("recovery OCI CLI/API-key configuration missing")
    return run([str(OCI), "--config-file", str(API_CONFIG), "--region", REGION, *args], 90)


def admin_valid() -> bool:
    if not OCI.is_file() or not ADMIN_CONFIG.is_file():
        return False
    try:
        run([str(OCI), "--config-file", str(ADMIN_CONFIG), "--profile",
             "KATCHA_VAULT_ADMIN", "--auth", "security_token", "session", "validate"])
        return True
    except AccessError:
        return False


def reauth() -> None:
    if admin_valid():
        print("vault_admin_session: VALID")
        return
    if not sys.stdin.isatty():
        raise AccessError("administrator browser authentication requires a terminal")
    run([str(OCI), "session", "authenticate", "--region", REGION,
         "--profile-name", "KATCHA_VAULT_ADMIN"], 600)
    if not admin_valid():
        raise AccessError("browser reauthentication did not validate")
    print("vault_admin_session: VALID")


def variable(name: str) -> str:
    return run(["gh", "variable", "get", name, "-R", REPO], 30)


def valid_ocid(value: str, kind: str) -> bool:
    return bool(re.fullmatch("ocid1\\." + kind + "\\.[A-Za-z0-9._-]+", value))


def target() -> tuple[str, str]:
    bastion, ip = variable("KATCHA_OCI_BASTION_ID"), variable("KATCHA_OCI_PRIMARY_PRIVATE_IP")
    if not valid_ocid(bastion, "bastion"):
        raise AccessError("existing Bastion ID invalid or missing")
    try:
        address = ipaddress.ip_address(ip)
    except ValueError as exc:
        raise AccessError("private target IP invalid") from exc
    if address.version != 4 or not address.is_private or address.is_loopback:
        raise AccessError("target must be a private IPv4 address")
    return bastion, ip


def listening() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=1):
            return True
    except OSError:
        return False


def load() -> dict | None:
    path = STATE_DIR / "session.json"
    if not path.exists():
        return None
    if path.is_symlink():
        raise AccessError("refusing symbolic link in tunnel metadata")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AccessError("managed tunnel state unreadable; investigate manually") from exc
    if not isinstance(state, dict):
        raise AccessError("invalid managed tunnel state")
    return state


def save(state: dict) -> None:
    path = STATE_DIR / "session.json.new"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, (json.dumps(state, sort_keys=True) + "\n").encode())
    finally:
        os.close(fd)
    os.replace(path, STATE_DIR / "session.json")


def owned(state: dict) -> bool:
    pid, sid = state.get("pid"), state.get("session_id")
    if not isinstance(pid, int) or pid < 2 or not isinstance(sid, str):
        return False
    try:
        cmdline = Path("/proc/" + str(pid) + "/cmdline").read_bytes().replace(b"\x00", b" ")
    except OSError:
        return False
    host = (sid + "@host.bastion." + REGION + ".oci.oraclecloud.com").encode()
    return (cmdline.startswith(b"ssh ") or b"/ssh " in cmdline) and (
        host in cmdline and b"127.0.0.1:22022:" in cmdline
    )


def handshake() -> bool:
    """Pinned real primary SSH handshake, not just a listener check."""
    if not (listening() and TARGET_KEY.is_file() and TARGET_HOSTKEYS.is_file()):
        return False
    try:
        run(["ssh", "-F", "/dev/null", "-4", "-i", str(TARGET_KEY),
             "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
             "-o", "StrictHostKeyChecking=yes",
             "-o", "UserKnownHostsFile=" + str(TARGET_HOSTKEYS),
             "-o", "ConnectTimeout=6", "-p", str(PORT), "ubuntu@127.0.0.1", "true"], 12)
        return True
    except AccessError:
        return False


@contextmanager
def lock():
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    if STATE_DIR.is_symlink():
        raise AccessError("tunnel directory may not be a symlink")
    os.chmod(STATE_DIR, 0o700)
    fd = os.open(STATE_DIR / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def delete_session(sid: str) -> None:
    if not valid_ocid(sid, "bastionsession"):
        raise AccessError("invalid managed session ID; no OCI deletion attempted")
    oci("bastion", "session", "delete", "--session-id", sid, "--force")


def stop_owned(state: dict) -> None:
    if owned(state):
        os.kill(state["pid"], signal.SIGTERM)
        for _ in range(15):
            if not owned(state):
                return
            time.sleep(0.1)


def disconnect_locked() -> None:
    state = load()
    if state is None:
        raise AccessError("no managed session; unrelated tunnels left untouched")
    if listening() and not owned(state):
        raise AccessError("port 22022 has an unrecognized listener; refusing cleanup")
    sid = state.get("session_id")
    if not isinstance(sid, str):
        raise AccessError("invalid session state; refusing to delete")
    stop_owned(state)
    delete_session(sid)
    (STATE_DIR / "session.json").unlink()
    for name in ("session_key", "session_key.pub"):
        (STATE_DIR / name).unlink(missing_ok=True)
    print("bastion_tunnel: DISCONNECTED; only managed session deleted")


def connect() -> None:
    with lock():
        state = load()
        if state and owned(state) and handshake():
            print("bastion_tunnel: CONNECTED; reused existing managed tunnel")
            return
        if listening():
            raise AccessError("port 22022 in use by unverified listener; no session created")
        if state:
            disconnect_locked()
        if not TARGET_KEY.is_file() or not TARGET_HOSTKEYS.is_file():
            raise AccessError("target SSH key or pinned target_known_hosts missing; no OCI session created")
        bastion, ip = target()
        if oci("bastion", "bastion", "get", "--bastion-id", bastion,
               "--query", 'data."lifecycle-state"', "--raw-output") != "ACTIVE":
            raise AccessError("existing Bastion is not ACTIVE")
        key = STATE_DIR / "session_key"
        if key.exists() or key.is_symlink():
            raise AccessError("unexpected ephemeral key exists; investigate manually")
        run(["ssh-keygen", "-q", "-t", "rsa", "-b", "3072", "-N", "", "-f", str(key)])
        os.chmod(key, 0o600)
        sid = ""
        child: subprocess.Popen | None = None
        try:
            sid = oci("bastion", "session", "create-port-forwarding",
                      "--bastion-id", bastion, "--display-name", "katcha-operator-admin",
                      "--ssh-public-key-file", str(key) + ".pub",
                      "--target-private-ip", ip, "--target-port", "22",
                      "--session-ttl", "10800", "--query", "data.id", "--raw-output")
            if not valid_ocid(sid, "bastionsession"):
                raise AccessError("Bastion session result ambiguous; inspect sessions before retrying")
            save({"session_id": sid, "pid": None, "target_ip": ip})
            for _ in range(45):
                phase = oci("bastion", "session", "get", "--session-id", sid,
                            "--query", 'data."lifecycle-state"', "--raw-output")
                if phase == "ACTIVE":
                    break
                if phase in ("DELETED", "FAILED", "EXPIRED", "CANCELED"):
                    raise AccessError("Bastion session terminal: " + phase)
                time.sleep(2)
            else:
                raise AccessError("Bastion session activation timed out")
            host = sid + "@host.bastion." + REGION + ".oci.oraclecloud.com"
            for _ in range(24):
                log_fd = os.open(STATE_DIR / "ssh.log",
                                 os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
                with os.fdopen(log_fd, "wb") as log:
                    child = subprocess.Popen(
                        ["ssh", "-F", "/dev/null", "-4", "-i", str(key),
                         "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
                         "-o", "IdentityAgent=none", "-o", "HostKeyAlgorithms=+ssh-rsa",
                         "-o", "PubkeyAcceptedAlgorithms=+ssh-rsa",
                         "-o", "StrictHostKeyChecking=accept-new",
                         "-o", "UserKnownHostsFile=" + str(STATE_DIR / "bastion_known_hosts"),
                         "-o", "ExitOnForwardFailure=yes",
                         "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
                         "-N", "-L", "127.0.0.1:" + str(PORT) + ":" + ip + ":22",
                         "-p", "22", host],
                        stdin=subprocess.DEVNULL, stdout=log,
                        stderr=subprocess.STDOUT, start_new_session=True,
                    )
                save({"session_id": sid, "pid": child.pid, "target_ip": ip})
                time.sleep(2)
                if child.poll() is None and listening():
                    for _ in range(6):
                        if handshake():
                            print("bastion_tunnel: CONNECTED on 127.0.0.1:22022")
                            print("Used existing Bastion; session TTL three hours; no VM/disk created")
                            return
                        time.sleep(2)
                    raise AccessError("SSH listener ready but pinned primary handshake failed")
                if child.poll() is None:
                    child.terminate()
                    child.wait(timeout=8)
                child = None
                log_data = (STATE_DIR / "ssh.log").read_text(
                    encoding="utf-8", errors="replace"
                )
                if "Permission denied (publickey)" not in log_data:
                    raise AccessError("Bastion SSH failed; inspect protected ssh.log locally")
                time.sleep(3)
            raise AccessError("Bastion SSH key propagation timed out")
        except Exception:
            if child is not None and child.poll() is None:
                child.terminate()
            if sid and valid_ocid(sid, "bastionsession"):
                try:
                    delete_session(sid)
                except AccessError:
                    print("NOTICE: managed OCI session cleanup failed; state retained", file=sys.stderr)
                    raise
                (STATE_DIR / "session.json").unlink(missing_ok=True)
            key.unlink(missing_ok=True)
            (STATE_DIR / "session_key.pub").unlink(missing_ok=True)
            raise


def status() -> bool:
    valid = admin_valid()
    print("vault_admin_session:", "VALID" if valid else "NEEDS_BROWSER_LOGIN")
    state = load()
    if state and owned(state) and listening():
        connected = handshake()
        print("bastion_tunnel:", "CONNECTED" if connected else "LISTENING_UNVERIFIED")
    elif listening():
        connected = False
        print("bastion_tunnel: UNKNOWN_LISTENER; not owned by this wrapper")
    else:
        connected = False
        print("bastion_tunnel:", "STALE_MANAGED" if state else "MISSING")
    for switch in SWITCHES:
        try:
            state_value = variable(switch)
            print(switch + ":", "SAFE_FALSE" if state_value.lower() == "false" else "REVIEW")
        except AccessError:
            print(switch + ": NOT_VERIFIED")
    return valid and connected


def audit() -> bool:
    success = True
    for name, command, markers in [
        ("IAM", "audit-oci-recovery-readiness.py", ["vault_admin_session: VALID",
                                                    "candidate_four_secret_only_grant: PASS"]),
        ("CAPACITY", "audit-oci-free-candidate-capacity.py",
         ["READ_ONLY_CAPACITY_PREFLIGHT_COMPLETE"]),
    ]:
        arguments = [sys.executable, str(SCRIPTS / command)]
        if name == "IAM":
            arguments.append("--iam-only")
        try:
            output = run(arguments, timeout=240)
            print("\n=== " + name + " ===\n" + output)
            if any(m not in output for m in markers) or any(
                bad in output for bad in ("NOT_VERIFIED", "REVIEW_", "CAPACITY_PREFLIGHT_STOP")
            ):
                success = False
        except AccessError as exc:
            print(name + ": BLOCKED (" + str(exc) + ")")
            success = False
    if handshake():
        try:
            output = run([sys.executable, str(SCRIPTS / "audit-oci-recovery-readiness.py"),
                          "--backups-only"], timeout=240)
            print("\n=== BACKUPS ===\n" + output)
            if output.count("SUCCESS_FRESH") < 2 or any(
                bad in output for bad in ("UNVERIFIED", "HOST_AUDIT_UNAVAILABLE",
                                           "TIMER_NOT_READY", "NO_SUCCESS_MARKER",
                                           "SUCCESS_STALE", "SUCCESS_TIMESTAMP_INVALID")
            ):
                success = False
        except AccessError as exc:
            print("BACKUPS: BLOCKED (" + str(exc) + ")")
            success = False
    else:
        print("BACKUPS: NOT_VERIFIED; run connect to establish a verified tunnel")
        success = False
    print("Overall read-only evidence:", "VERIFIED" if success else "INCOMPLETE")
    return success


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "connect", "disconnect", "audit",
                                             "doctor", "reauth"))
    command = parser.parse_args(argv).command
    try:
        if command == "status":
            return 0 if status() else 2
        if command == "doctor":
            ok = status()
            print("Next: reauth for expired admin; connect for missing tunnel; "
                  "audit for full read-only evidence.")
            return 0 if ok else 2
        if command == "connect":
            connect()
        elif command == "disconnect":
            with lock():
                disconnect_locked()
        elif command == "reauth":
            reauth()
        elif command == "audit":
            return 0 if audit() else 2
    except (AccessError, OSError) as exc:
        print("OCI_OPERATOR_STOP: " + str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
