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
from datetime import datetime, timezone
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
GITHUB_RECOVERY_HINT = (
    "Check 'gh auth status -h github.com' and "
    "'gh variable list -R Deveadra/katcha'; if needed, run "
    "'gh auth login -h github.com'. Check for an expired GH_TOKEN or "
    "GITHUB_TOKEN overriding the saved login. No OCI resources were changed."
)


class AccessError(Exception):
    pass


# Persist only allowlisted metadata; NEVER raw provider stderr, command arguments,
# GitHub tokens, OCI session identifiers, IP addresses, or interactive login URLs.
EVENTS = frozenset({
    "command_start", "command_finished", "command_failed",
    "github_request_failed", "github_variable_unavailable",
    "bastion_active", "bastion_access_mismatch", "session_created",
    "session_active", "ssh_forward_attempt", "ssh_forward_failed",
    "ssh_handshake_failed", "tunnel_verified", "session_cleanup_failed",
})
REASONS = frozenset({
    "github_unauthorized", "github_forbidden", "github_not_found",
    "github_failed", "github_timeout", "github_unavailable",
    "bastion_mismatch", "session_unknown", "ssh_failed", "target_unverified",
})
ACTIONS = frozenset({
    "status", "connect", "disconnect", "audit", "doctor", "reauth",
    "repair-access", "metrics", "github-doctor", "diagnostics",
})


def log_event(event: str, *, action: str = "", reason: str = "") -> None:
    """Append small, token-free events to an owner-only local log.

    Logging is best-effort so diagnostic I/O cannot block safe OCI operations.
    Paths are fixed. No caller-provided message or exception text is persisted.
    """
    if event not in EVENTS or (action and action not in ACTIONS) or (
        reason and reason not in REASONS
    ):
        return
    record = {"utc": datetime.now(timezone.utc).isoformat(), "event": event}
    if action:
        record["action"] = action
    if reason:
        record["reason"] = reason
    try:
        if STATE_DIR.is_symlink():
            return
        STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        if STATE_DIR.is_symlink():
            return
        # Existing operator state directory is already owner-only. Do not make
        # log files group/world-readable under permissive umask settings.
        fd = os.open(STATE_DIR / "operator-events.jsonl",
                     os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, (json.dumps(record, sort_keys=True) + "\n").encode())
        finally:
            os.close(fd)
    except OSError:
        pass


def github_failure(stderr: str) -> str:
    """Classify provider errors without ever exposing their raw output."""
    if "HTTP 401" in stderr or "Bad credentials" in stderr:
        return "github_unauthorized"
    if "HTTP 403" in stderr or "Resource not accessible" in stderr:
        return "github_forbidden"
    if "HTTP 404" in stderr:
        return "github_not_found"
    return "github_failed"


def diagnostics() -> bool:
    """Display only verified fields from the latest local structured events."""
    path = STATE_DIR / "operator-events.jsonl"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            size = os.fstat(fd).st_size
            offset = max(0, size - 131072)
            os.lseek(fd, offset, os.SEEK_SET)
            raw = os.read(fd, min(size, 131072)).decode("utf-8", "replace")
        finally:
            os.close(fd)
    except OSError:
        print("operator_events: NO_LOCAL_LOG (run a command to record events)")
        return False
    if offset:
        raw = raw.partition("\n")[2]  # ignore truncated first record
    rows = []
    for line in raw.splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if not isinstance(value, dict) or value.get("event") not in EVENTS:
            continue
        if value.get("action", "") not in ACTIONS | {""}:
            continue
        if value.get("reason", "") not in REASONS | {""}:
            continue
        if not isinstance(value.get("utc"), str):
            continue
        rows.append({key: value[key] for key in ("utc", "event", "action", "reason")
                     if key in value})
    print("operator_events: sanitized; last " + str(min(len(rows), 25)) + " events")
    for row in rows[-25:]:
        print(json.dumps(row, sort_keys=True))
    return bool(rows)


def github_doctor() -> bool:
    """Read-only local GH auth and repository-variable access preflight."""
    for label, argv in (
        ("github_cli_auth", ["gh", "auth", "status", "-h", "github.com"]),
        ("github_repo_variables", ["gh", "variable", "list", "-R", REPO,
                                   "--json", "name"]),
    ):
        try:
            run(argv, 30)
        except AccessError as exc:
            print(label + ": FAILED (" + str(exc) + ")")
            print("Next: bash scripts/katcha-oci-startup.sh github-login; "
                  "finish the device flow in WSL and check GH_TOKEN/GITHUB_TOKEN.")
            return False
        print(label + ": PASS")
    return True


def run(args: list[str], timeout: int = 45) -> str:
    try:
        result = subprocess.run(args, capture_output=True, text=True, check=False, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        if Path(args[0]).name == "gh":
            reason = ("github_timeout" if isinstance(exc, subprocess.TimeoutExpired)
                      else "github_unavailable")
            log_event("github_request_failed", reason=reason)
            raise AccessError("GitHub CLI failed (" + reason + "); check WSL "
                              "installation/network and retry") from exc
        raise AccessError("local command unavailable or timed out: " + Path(args[0]).name) from exc
    if result.returncode:
        if Path(args[0]).name == "gh":
            reason = github_failure(result.stderr or "")
            log_event("github_request_failed", reason=reason)
            raise AccessError("GitHub CLI failed (" + reason + ", exit "
                              + str(result.returncode) + ")")
        raise AccessError("command failed: " + Path(args[0]).name
                          + " (exit " + str(result.returncode) + ")")
    return result.stdout.strip()


def run_interactive(args: list[str], timeout: int = 300) -> None:
    """Use the user's terminal for a browser/MFA flow; never capture its prompts."""
    try:
        # Inherit stdin/stdout/stderr so OCI can display instructions and receive
        # verification input. Capturing stdout/stderr hides all login progress.
        result = subprocess.run(args, check=False, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise AccessError("interactive OCI login timed out; retry from a WSL terminal") from exc
    except OSError as exc:
        raise AccessError("interactive OCI login could not start") from exc
    if result.returncode:
        raise AccessError("interactive OCI login failed (exit "
                          + str(result.returncode) + ")")


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
    print("OCI browser sign-in may require a browser or manual verification.", flush=True)
    print("Follow the OCI CLI instructions shown below (five-minute limit).", flush=True)
    run_interactive([str(OCI), "session", "authenticate", "--region", REGION,
                     "--profile-name", "KATCHA_VAULT_ADMIN"])
    if not admin_valid():
        raise AccessError("browser reauthentication did not validate")
    print("vault_admin_session: VALID")


def variable(name: str) -> str:
    try:
        value = run(["gh", "variable", "get", name, "-R", REPO], 30)
    except AccessError as exc:
        log_event("github_variable_unavailable", reason="github_failed")
        raise AccessError(
            "GitHub Actions variable '" + name + "' unavailable: "
            + str(exc) + ". " + GITHUB_RECOVERY_HINT
        ) from exc
    if not value:
        raise AccessError(
            "GitHub Actions variable '" + name + "' is empty. "
            + GITHUB_RECOVERY_HINT
        )
    return value


def github_audit_ready() -> bool:
    """Check the prerequisite shared by both read-only OCI metadata audits."""
    try:
        run(["gh", "variable", "list", "-R", REPO, "--json", "name"], 30)
    except AccessError:
        print("audit_github_variables: UNAVAILABLE. " + GITHUB_RECOVERY_HINT)
        return False
    return True


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
    if address.version != 4 or not any(
        address in ipaddress.ip_network(network)
        for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    ):
        raise AccessError("target must be an RFC1918 private IPv4 address")
    return bastion, ip


def bastion_client_access(bastion: str) -> tuple[str, str]:
    """Read-only OCI CIDR allowlist versus observed HTTPS IPv4 egress.

    The HTTPS egress may differ from SSH when a split-tunnel VPN is in use;
    this is a diagnostic, not an authority to mutate security settings.
    """
    raw = oci("bastion", "bastion", "get", "--bastion-id", bastion)
    try:
        record = json.loads(raw)
        data = record["data"]
        cidrs = data["client-cidr-block-allow-list"]
        if not isinstance(cidrs, list) or not cidrs or any(
            not isinstance(item, str) for item in cidrs
        ):
            raise ValueError("invalid CIDR list")
        networks = [ipaddress.ip_network(item, strict=False) for item in cidrs]
    except (ValueError, KeyError, TypeError) as exc:
        raise AccessError("Bastion CIDR allowlist metadata unavailable; no changes made") from exc
    try:
        ip = ipaddress.IPv4Address(
            run(["curl", "-4", "-fsS", "--connect-timeout", "4", "--max-time", "10",
                 "https://api.ipify.org"], timeout=14)
        )
        if not ip.is_global:
            raise ValueError("non-global response")
    except (AccessError, ValueError) as exc:
        raise AccessError("public IPv4 egress cannot be verified; check VPN or IP service") from exc
    if any(ip in network for network in networks):
        return "ALLOWED", "Observed HTTPS IPv4 is in the OCI Bastion CIDR allowlist"
    return "MISMATCH", (
        "Observed HTTPS IPv4 is outside OCI Bastion CIDR allowlist. "
        "Oracle documents 'closed by remote host' for this case. "
        "Review your current egress/VPN and Bastion allowlist; changing it needs "
        "a new session. No Bastion settings were changed."
    )


def diagnose_bastion_access() -> str:
    try:
        bastion, _ = target()
        result, reason = bastion_client_access(bastion)
        print("bastion_client_allowlist:", result)
        print(reason)
        return result
    except AccessError as exc:
        print("bastion_client_allowlist: UNVERIFIED (" + str(exc) + ")")
        return "UNVERIFIED"



def current_public_ipv4() -> ipaddress.IPv4Address:
    """Require agreement between two independent external IPv4 reflectors."""
    observed = []
    for url in ("https://api.ipify.org", "https://checkip.amazonaws.com"):
        try:
            address = ipaddress.IPv4Address(
                run(["curl", "-4", "-fsS", "--connect-timeout", "4",
                     "--max-time", "10", url], timeout=14).strip()
            )
        except (AccessError, ValueError) as exc:
            raise AccessError("public IPv4 could not be independently verified; "
                              "Bastion allowlist unchanged") from exc
        if not address.is_global:
            raise AccessError("public IPv4 reflector returned non-global address; "
                              "Bastion allowlist unchanged")
        observed.append(address)
    if observed[0] != observed[1]:
        raise AccessError("public IPv4 sources disagree; inspect proxy/VPN/egress before "
                          "changing the allowlist")
    return observed[0]


def active_bastion_single_host_rule(bastion: str) -> str:
    """Only allow replacing the bootstrap's exact single-host /32 allowlist."""
    try:
        response = json.loads(oci("bastion", "bastion", "get", "--bastion-id", bastion))
        record = response["data"]
        if record["id"] != bastion or record["lifecycle-state"] != "ACTIVE":
            raise ValueError("wrong resource or inactive Bastion")
        rules = record["client-cidr-block-allow-list"]
        if not isinstance(rules, list) or len(rules) != 1 or not isinstance(rules[0], str):
            raise ValueError("expected exactly one existing rule")
        network = ipaddress.IPv4Network(rules[0], strict=True)
        if network.prefixlen != 32:
            raise ValueError("existing rule is not a /32")
    except (AccessError, ValueError, KeyError, TypeError) as exc:
        raise AccessError("Bastion identity, state, or single IPv4 /32 allowlist "
                          "could not be verified; no changes made") from exc
    return str(network)


def repair_access() -> None:
    """Explicitly approved mutation: replace one existing Bastion /32 with current IPv4."""
    with lock():
        if listening():
            raise AccessError("port 22022 is already listening; inspect the existing "
                              "tunnel before changing Bastion access")
        bastion, _ = target()  # Target and existing Bastion from GitHub variables only.
        old = active_bastion_single_host_rule(bastion)
        new = str(ipaddress.IPv4Network(str(current_public_ipv4()) + "/32"))
        print("Existing OCI Bastion: verified ACTIVE; single-host allowlist", flush=True)
        print("Current permitted IPv4:", old, flush=True)
        print("Proposed replacement:", new, flush=True)
        print("Scope: existing Bastion client IP allowlist only; no VM, disk, "
              "network rules, or production changes.", flush=True)
        if old == new:
            print("BASTION_ALLOWLIST_ALREADY_CURRENT; no update needed")
            return
        if not sys.stdin.isatty():
            raise AccessError("interactive confirmation required; no changes made")
        try:
            approval = input("Type UPDATE to replace the existing /32 rule: ").strip()
        except EOFError as exc:
            raise AccessError("confirmation unavailable; no changes made") from exc
        if approval != "UPDATE":
            raise AccessError("confirmation not received; no changes made")
        # Recheck both Oracle resource metadata and the observed egress IP
        # before making exactly one OCI update. Never broaden to /0.
        if active_bastion_single_host_rule(bastion) != old:
            raise AccessError("Bastion allowlist changed during approval; no update attempted")
        if str(ipaddress.IPv4Network(str(current_public_ipv4()) + "/32")) != new:
            raise AccessError("public IPv4 changed during approval; no update attempted")
        try:
            oci("bastion", "bastion", "update", "--bastion-id", bastion,
                "--client-cidr-list", json.dumps([new]), "--force")
        except AccessError as exc:
            raise AccessError("Bastion update outcome UNKNOWN; inspect using 'doctor' "
                              "before any retry") from exc
        # The update can be asynchronous. Poll read-only instead of issuing
        # another mutation or interpreting transient UPDATING as failure.
        verified = False
        for _ in range(45):
            try:
                actual = active_bastion_single_host_rule(bastion)
            except AccessError:
                actual = None
            if actual == new:
                verified = True
                break
            if actual is not None and actual != old:
                raise AccessError("Bastion allowlist changed unexpectedly after update; "
                                  "inspect before attempting another change")
            time.sleep(2)
        if not verified:
            raise AccessError("Bastion update outcome still unverified after 90 seconds; "
                              "run 'doctor' before retrying")
        print("BASTION_ALLOWLIST_UPDATED: verified exact " + new)
        print("Next: run 'connect' to create a fresh Bastion session, then 'audit'.")



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
    phase = oci("bastion", "session", "get", "--session-id", sid,
                "--query", 'data."lifecycle-state"', "--raw-output")
    if phase not in ("EXPIRED", "DELETED", "FAILED", "CANCELED"):
        delete_session(sid)
    (STATE_DIR / "session.json").unlink()
    for name in ("session_key", "session_key.pub"):
        (STATE_DIR / name).unlink(missing_ok=True)
    print("bastion_tunnel: DISCONNECTED; only managed session deleted")



def wait_for_forward_listener(child: subprocess.Popen, timeout: float = 15.0) -> bool:
    """Allow SSH to negotiate before deciding whether the forward failed."""
    deadline = time.monotonic() + timeout
    while child.poll() is None:
        if listening():
            return child.poll() is None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.25, remaining))
    return False


def connect() -> None:
    with lock():
        state = load()
        if state and owned(state) and handshake():
            log_event("tunnel_verified", action="connect")
            print("bastion_tunnel: CONNECTED; reused existing managed tunnel")
            return
        if state and owned(state) and listening():
            # Do not recycle a healthy OCI session because the target is down.
            phase = oci("bastion", "session", "get", "--session-id", state["session_id"],
                        "--query", 'data."lifecycle-state"', "--raw-output")
            if phase == "ACTIVE":
                raise AccessError("managed Bastion session active but target SSH is "
                                  "unverified; inspect primary/host key without replacing it")
            if phase not in ("EXPIRED", "DELETED", "FAILED", "CANCELED"):
                raise AccessError("managed Bastion session state requires review: " + phase)
            disconnect_locked()
            state = None
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
        # Oracle's documented 'closed by remote host' failure indicates that
        # current public egress may not match the Bastion client CIDR allowlist.
        try:
            allow_status, reason = bastion_client_access(bastion)
        except AccessError as exc:
            print("NOTE: Bastion allowlist preflight unavailable: " + str(exc),
                  file=sys.stderr)
        else:
            if allow_status == "MISMATCH":
                log_event("bastion_access_mismatch", reason="bastion_mismatch")
                raise AccessError(reason + " Refusing to create a doomed session.")
        log_event("bastion_active", action="connect")
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
            log_event("session_created", action="connect")
            for _ in range(45):
                phase = oci("bastion", "session", "get", "--session-id", sid,
                            "--query", 'data."lifecycle-state"', "--raw-output")
                if phase == "ACTIVE":
                    log_event("session_active", action="connect")
                    break
                if phase in ("DELETED", "FAILED", "EXPIRED", "CANCELED"):
                    raise AccessError("Bastion session terminal: " + phase)
                time.sleep(2)
            else:
                raise AccessError("Bastion session activation timed out")
            host = sid + "@host.bastion." + REGION + ".oci.oraclecloud.com"
            for _ in range(24):
                log_event("ssh_forward_attempt", action="connect")
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
                if wait_for_forward_listener(child):
                    for _ in range(6):
                        if handshake():
                            log_event("tunnel_verified", action="connect")
                            print("bastion_tunnel: CONNECTED on 127.0.0.1:22022")
                            print("Used existing Bastion; session TTL three hours; no VM/disk created")
                            return
                        time.sleep(2)
                    log_event("ssh_handshake_failed", action="connect",
                              reason="target_unverified")
                    raise AccessError("SSH listener ready but pinned primary handshake failed")
                if child.poll() is None:
                    child.terminate()
                    child.wait(timeout=8)
                child = None
                log_data = (STATE_DIR / "ssh.log").read_text(
                    encoding="utf-8", errors="replace"
                )
                if "Connection to " in log_data and "closed by remote host" in log_data:
                    log_event("ssh_forward_failed", action="connect", reason="bastion_mismatch")
                    raise AccessError(
                        "Bastion remote host closed SSH before the local tunnel was established. "
                        "Run 'doctor' to compare current IPv4 egress with the Bastion CIDR allowlist. "
                        "Do not create repeated sessions or change network settings blindly."
                    )
                if "Permission denied (publickey)" not in log_data:
                    log_event("ssh_forward_failed", action="connect", reason="ssh_failed")
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
                    log_event("session_cleanup_failed", action="connect",
                              reason="session_unknown")
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
    github_warning = False
    for switch in SWITCHES:
        try:
            state_value = variable(switch)
            print(switch + ":", "SAFE_FALSE" if state_value.lower() == "false" else "REVIEW")
        except AccessError as exc:
            print(switch + ": NOT_VERIFIED")
            if not github_warning:
                print("github_variables: UNAVAILABLE (" + str(exc) + ")")
                github_warning = True
    return valid and connected


def audit() -> bool:
    success = True
    # Both administrator metadata audits require the same short-lived OCI
    # browser security token. Diagnose it once without running probes that
    # would only return an opaque Python exit code.
    admin_ready = admin_valid()
    if not admin_ready:
        print("audit_admin_session: NEEDS_BROWSER_LOGIN")
    github_ready = github_audit_ready() if admin_ready else False
    for name, command, markers in [
        ("IAM", "audit-oci-recovery-readiness.py", ["vault_admin_session: VALID",
                                                    "candidate_four_secret_only_grant: PASS"]),
        ("CAPACITY", "audit-oci-free-candidate-capacity.py",
         ["READ_ONLY_CAPACITY_PREFLIGHT_COMPLETE"]),
    ]:
        if not admin_ready:
            print(name + ": BLOCKED (administrator session invalid or unavailable; "
                  "run 'bash scripts/katcha-oci-admin.sh reauth')")
            success = False
            continue
        if not github_ready:
            print(name + ": BLOCKED (GitHub Actions variables unavailable; "
                  "no OCI metadata audit attempted)")
            success = False
            continue
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
            # Preserve generic/redacted command errors: these scripts may call
            # OCI and GitHub, so raw stderr and exception chains are not safe
            # to print. Give an exact diagnostic command for the operator.
            diagnostic = (
                "python3 scripts/audit-oci-recovery-readiness.py --iam-only"
                if name == "IAM"
                else "python3 scripts/audit-oci-free-candidate-capacity.py"
            )
            print(name + ": BLOCKED (" + str(exc) + "; diagnose with '" +
                  diagnostic + "' in your WSL terminal)")
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
    parser.add_argument("command", nargs="?", default="status",
                        choices=("status", "connect", "disconnect", "audit",
                                 "doctor", "reauth", "repair-access", "metrics",
                                 "github-doctor", "diagnostics"))
    command = parser.parse_args(argv).command
    log_event("command_start", action=command)
    try:
        if command == "github-doctor":
            return 0 if github_doctor() else 2
        if command == "diagnostics":
            return 0 if diagnostics() else 2
        if command == "status":
            return 0 if status() else 2
        if command == "doctor":
            ok = status()
            access = diagnose_bastion_access()
            print("Next: reauth for expired admin; investigate Bastion allowlist "
                  "mismatches before creating new sessions; audit for recovery evidence.")
            return 0 if ok and access == "ALLOWED" else 2
        if command == "connect":
            connect()
        elif command == "disconnect":
            with lock():
                disconnect_locked()
        elif command == "reauth":
            reauth()
        elif command == "repair-access":
            repair_access()
        elif command == "audit":
            return 0 if audit() else 2
        elif command == "metrics":
            # The helper validates OCI CLI output and only prints filtered data.
            # Inherit terminal output; never hide useful metrics errors.
            try:
                result = subprocess.run(
                    [sys.executable, str(SCRIPTS / "oci-primary-metrics.py")],
                    check=False, timeout=360,
                )
            except subprocess.TimeoutExpired as exc:
                raise AccessError("read-only OCI metrics command timed out") from exc
            return result.returncode
    except KeyboardInterrupt:
        print("\nOCI_OPERATOR_STOP: interrupted by operator", file=sys.stderr)
        return 130
    except (AccessError, OSError) as exc:
        log_event("command_failed", action=command)
        print("OCI_OPERATOR_STOP: " + str(exc), file=sys.stderr)
        return 2
    finally:
        log_event("command_finished", action=command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
