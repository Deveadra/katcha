#!/usr/bin/env python3
"""Read-only public production probes. Never uses Bastion, OCI or operator secrets."""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

RELEASE_SHA = re.compile(r"[0-9a-f]{40}\Z")
EXPECTED = {
    "/v1/health/live": ("status", "ok"),
    "/v1/health/ready": ("status", "ok"),
    "/v1/health/release": ("release_sha", None),
    "/healthz": ("service", "katcha-recovery-coordinator"),
}


class ProbeError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise ProbeError("unexpected redirect")


def checked_origin(value: str, hostname: str) -> str:
    parts = urllib.parse.urlsplit(value)
    if (parts.scheme != "https" or parts.hostname != hostname or parts.port not in (None, 443)
            or parts.username or parts.password or parts.query or parts.fragment):
        raise ProbeError("probe target must be the exact HTTPS production hostname")
    if parts.path not in ("", "/", "/v1/health/ready"):
        raise ProbeError("unexpected probe target path")
    return f"https://{hostname}"


def probe(url: str, field: str, expected: str | None, opener=None) -> dict:
    opener = opener or urllib.request.build_opener(NoRedirect)
    request = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "Cache-Control": "no-cache",
        "User-Agent": "Katcha-Unattended-Acceptance/1",
    })
    try:
        with opener.open(request, timeout=8) as response:
            if response.status != 200:
                raise ProbeError(f"HTTP {response.status}")
            if response.headers.get_content_type() != "application/json":
                raise ProbeError("expected application/json")
            payload = json.loads(response.read(4097).decode("utf-8"))
    except (OSError, TimeoutError, ValueError, UnicodeError, urllib.error.URLError) as exc:
        raise ProbeError("request, TLS, or JSON validation failed") from exc
    if not isinstance(payload, dict):
        raise ProbeError("unexpected JSON shape")
    value = payload.get(field)
    if expected is None:
        if not isinstance(value, str) or not RELEASE_SHA.fullmatch(value):
            raise ProbeError("release identity is missing or invalid")
    elif value != expected:
        raise ProbeError(f"unexpected {field}")
    return {field: value}


def check(app_url: str, recovery_url: str) -> dict:
    app = checked_origin(app_url, "app.katcha.stream")
    recovery = checked_origin(recovery_url, "recovery.katcha.stream")
    results = {}
    ok = True
    for label, origin, path in (
        ("live", app, "/v1/health/live"),
        ("ready", app, "/v1/health/ready"),
        ("release", app, "/v1/health/release"),
        ("recovery", recovery, "/healthz"),
    ):
        field, expected = EXPECTED[path]
        try:
            result = probe(origin + path, field, expected)
            results[label] = {"ok": True, **result}
        except ProbeError as exc:
            ok = False
            results[label] = {"ok": False, "reason": str(exc)}
    return {
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "PASS" if ok else "FAIL",
        "source": "external-github-runner",
        "checks": results,
        "limitations": [
            "Public endpoints do not prove background workers, backups, or scheduled publishing.",
            "No OCI instance, Cloudflare configuration, or disaster-recovery failover tested.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-url", default="https://app.katcha.stream")
    parser.add_argument("--recovery-url", default="https://recovery.katcha.stream")
    args = parser.parse_args()
    try:
        result = check(args.app_url, args.recovery_url)
    except ProbeError as exc:
        result = {"status": "FAIL", "reason": str(exc)}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
