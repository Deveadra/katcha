#!/usr/bin/env python3
"""Read-only OCI deployment receipt notifier; never opens deployment gates.

Runs independently of the deploy poll timer, even when both authorization
switches are off. A root-private delivery ledger avoids repeated alerts.
"""
from __future__ import annotations

import fcntl
import importlib.util
import json
import os
import re
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path("/srv/katcha/deployments")
RECEIPTS = ROOT / "requests"
DELIVERED = ROOT / "notifications"
ENV = Path("/etc/katcha/katcha.env")
POLLER = Path("/usr/local/sbin/katcha-production-deploy-poll")
HOST = "katcha-prod-primary-001"
RELEASE_URL = "http://127.0.0.1:8000/v1/health/release"
GITHUB_URL = "https://github.com/Deveadra/katcha"
SHA = re.compile(r"[0-9a-f]{40}\Z")
NONCE = re.compile(r"[0-9a-f]{32}\Z")
TOKEN = re.compile(r"[0-9]+:[A-Za-z0-9_-]+\Z")
CHAT = re.compile(r"-?[0-9]+\Z")


def root_private(path: Path, *, directory: bool = False) -> bool:
    if path.is_symlink() or (not path.is_dir() if directory else not path.is_file()):
        return False
    stat = path.stat()
    return stat.st_uid == 0 and not (stat.st_mode & 0o077)


def configuration(path: Path = ENV) -> tuple[str, str, str] | None:
    if not root_private(path):
        return None
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if sep and key in {
            "KATCHA_TELEGRAM_BOT_TOKEN",
            "KATCHA_TELEGRAM_CHAT_ID",
            "KATCHA_TELEGRAM_THREAD_ID",
        }:
            if key in values:
                return None
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            values[key] = value
    token = values.get("KATCHA_TELEGRAM_BOT_TOKEN", "")
    chat = values.get("KATCHA_TELEGRAM_CHAT_ID", "")
    thread = values.get("KATCHA_TELEGRAM_THREAD_ID", "")
    if not TOKEN.fullmatch(token) or not CHAT.fullmatch(chat):
        return None
    if thread and (not CHAT.fullmatch(thread) or thread.startswith("-")):
        return None
    return token, chat, thread


def telegram_send(credentials: tuple[str, str, str], message: str) -> None:
    token, chat, thread = credentials
    fields = {"chat_id": chat, "text": message, "disable_web_page_preview": "true"}
    if thread:
        fields["message_thread_id"] = thread
    request = urllib.request.Request(
        "https://api.telegram.org/bot" + token + "/sendMessage",
        data=urllib.parse.urlencode(fields).encode("utf-8"),
        method="POST",
    )
    # Never put a Telegram URL/token, HTTP exception or API body into logs.
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read(8192)
        if json.loads(raw).get("ok") is not True:
            raise RuntimeError("Telegram did not accept deployment alert")
    except Exception as exc:
        raise RuntimeError("Telegram deployment alert delivery failed") from None


def live_sha() -> str | None:
    try:
        git = subprocess.run(
            ["git", "-C", "/opt/katcha", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
        with urllib.request.urlopen(RELEASE_URL, timeout=8) as response:
            api = json.load(response)
        if SHA.fullmatch(git) and isinstance(api, dict) and api.get("release_sha") == git:
            return git
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


def audit_confirmed(sha: str) -> bool:
    # Executor's result.json must corroborate its successful receipt.
    try:
        matches = sorted(ROOT.glob("*-" + sha[:12] + "/result.json"), reverse=True)
        for result_path in matches:
            if result_path.is_symlink() or not root_private(result_path):
                continue
            result = json.loads(result_path.read_text(encoding="utf-8"))
            if isinstance(result, dict) and result.get("sha") == sha and result.get("status") == "DEPLOYED":
                return True
    except (OSError, ValueError):
        pass
    return False


def receipt_event(receipt: dict[str, Any], *, current_sha: str | None,
                  audited: bool, age: float) -> tuple[str, str] | None:
    sha = receipt.get("sha")
    request_id = receipt.get("id")
    if (not isinstance(sha, str) or not SHA.fullmatch(sha)
        or type(request_id) is not int or request_id <= 0):
        return None
    prefix = "Katcha production | " + sha[:12] + "\nRequest ID: " + str(request_id) + "\n"
    ref = "\nGitHub: " + GITHUB_URL + "/deployments"
    if receipt.get("status") == "FAILED_REVIEW_REQUIRED":
        return "failed", prefix + "DEPLOYMENT FAILED: review the OCI receipt and protected audit." + ref
    if receipt.get("status") == "STARTED":
        if age >= 8100:  # executor timeout (7200 s) + margin
            return "stalled", prefix + "DEPLOYMENT STALLED: STARTED receipt has no terminal result. Investigate before redispatch." + ref
        return None
    if receipt.get("status") != "SUCCEEDED":
        return None
    executor = receipt.get("executor_status")
    if executor == "DEPLOYED" and current_sha == sha and audited:
        return "verified", prefix + "DEPLOYMENT VERIFIED SUCCESSFUL: OCI receipt, DEPLOYED audit, Git checkout and live API all agree." + ref
    if executor == "ALREADY_CURRENT" and current_sha == sha:
        return "already-current", prefix + "RELEASE ALREADY CURRENT: OCI confirmed live release; no new cutover occurred." + ref
    if age >= 300:
        return "unverified", prefix + "DEPLOYMENT VERIFICATION REQUIRED: receipt exists but live SHA or executor audit does not confirm success." + ref
    return None


def events(now: float, *, current_sha: str | None) -> list[tuple[str, str]]:
    messages = []
    if not root_private(RECEIPTS, directory=True):
        return messages
    for path in sorted(RECEIPTS.glob("nonce-*.json")):
        nonce = path.name.removeprefix("nonce-").removesuffix(".json")
        if not NONCE.fullmatch(nonce) or not root_private(path):
            continue
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(receipt, dict):
                continue
            sha = receipt.get("sha")
            event = receipt_event(
                receipt, current_sha=current_sha,
                audited=bool(isinstance(sha, str) and audit_confirmed(sha)),
                age=max(0.0, now - path.stat().st_mtime),
            )
            if event is not None:
                code, message = event
                messages.append(("nonce-" + nonce + "-" + code, message))
        except (OSError, ValueError):
            continue
    return messages


def expired_request_events(now: int) -> list[tuple[str, str]]:
    # An authorization can expire unconsumed when the deployment poller is off.
    # Reuse the installed poller's HMAC validation; never trust GitHub fields alone.
    try:
        spec = importlib.util.spec_from_file_location("production_poller", POLLER)
        if spec is None or spec.loader is None or not root_private(POLLER):
            return []
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        key = module.key_from_file()
        feed = module.fetch_requests()
    except Exception:
        return []
    found = []
    for deployment in feed:
        try:
            raw = deployment.get("payload")
            payload = json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(payload, dict):
                continue
            expires = payload.get("expires_at")
            issued = payload.get("issued_at")
            if (type(expires) is not int or type(issued) is not int
                or not expires + 60 < now or not now - issued < 86400):
                continue
            verified = module.authorized_request(deployment, key, expires)
            receipt = RECEIPTS / ("nonce-" + verified["nonce"] + ".json")
            if receipt.exists():
                continue
            ident = verified["id"]
            found.append((
                "expired-" + verified["nonce"],
                "Katcha production | " + verified["sha"][:12] + "\n"
                + "SIGNED REQUEST EXPIRED WITHOUT A HOST RECEIPT. "
                + "No deployment is verified. Request ID: " + str(ident)
                + "\nGitHub: " + GITHUB_URL + "/deployments",
            ))
        except (ValueError, KeyError, TypeError, AttributeError, OSError):
            continue
    return found


def run() -> int:
    if os.geteuid() != 0 or os.uname().nodename != HOST:
        print("NOTIFIER_BLOCKED: wrong host or not root")
        return 2
    creds = configuration()
    if creds is None:
        print("NOTIFIER_NOT_CONFIGURED: enable Telegram in root-private Katcha env")
        return 0
    if not root_private(ROOT, directory=True):
        print("NOTIFIER_BLOCKED: deployment storage not private")
        return 2
    DELIVERED.mkdir(mode=0o700, exist_ok=True)
    if not root_private(DELIVERED, directory=True):
        print("NOTIFIER_BLOCKED: notification ledger not private")
        return 2
    lock = DELIVERED / ".lock"
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        now = time.time()
        pending = events(now, current_sha=live_sha()) + expired_request_events(int(now))
        delivered = 0
        failed = 0
        for key, message in pending:
            marker = DELIVERED / (key + ".sent")
            if marker.exists():
                continue
            try:
                telegram_send(creds, message)
                receipt_fd = os.open(
                    marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
                )
                with os.fdopen(receipt_fd, "w") as out:
                    out.write("delivered\n")
                    out.flush()
                    os.fsync(out.fileno())
                delivered += 1
            except FileExistsError:
                continue
            except (OSError, RuntimeError):
                failed += 1
        print("NOTIFIER_RESULT delivered=" + str(delivered) + " failed=" + str(failed))
        return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(run())
