#!/usr/bin/env python3
"""Guest-side OCI instance-principal Vault acceptance probe.

No administrative OCI API credentials, secret bodies, resource mutations, or
coordinator calls. Must be invoked on the separately approved isolated VM.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from typing import Callable

IMDS_URL = "http://169.254.169.254/opc/v2/instance/"
BUNDLES = (
    "KATCHA_OCI_PRODUCTION_ENV_SECRET_ID",
    "KATCHA_OCI_AWS_BUNDLE_SECRET_ID",
    "KATCHA_OCI_BACKUP_ENV_SECRET_ID",
    "KATCHA_OCI_RESTORE_ENV_SECRET_ID",
)
DENIAL_CODES = ("NotAuthorizedOrNotFound", "NotAuthorized", "Forbidden")
OCID = re.compile(r"^ocid1\.vaultsecret\.[A-Za-z0-9._-]+$")


class AcceptanceError(RuntimeError):
    pass


def identity(fetch: Callable | None = None) -> str:
    """Bind results to the intended live candidate, never the operator machine."""
    if fetch is None:
        fetch = urllib.request.urlopen
    request = urllib.request.Request(
        IMDS_URL, headers={"Authorization": "Bearer Oracle"}
    )
    try:
        with fetch(request, timeout=5) as response:
            data = json.load(response)
    except (OSError, ValueError, TypeError) as exc:
        raise AcceptanceError("instance metadata unavailable") from exc
    value = data.get("id") if isinstance(data, dict) else None
    if not isinstance(value, str) or not value.startswith("ocid1.instance."):
        raise AcceptanceError("invalid OCI instance metadata")
    return value


def command(
    args: list[str],
    run: Callable = subprocess.run,
    *,
    timeout: float = 60,
) -> tuple[bool, str]:
    argv = ["oci", "--auth", "instance_principal", *args]
    try:
        result = run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AcceptanceError("instance-principal OCI CLI unavailable or timed out") from exc
    if result.returncode == 0:
        # The caller selects only the secret ID, never bundle content.
        return True, result.stdout.strip()
    stderr = result.stderr or ""
    if any(code in stderr for code in DENIAL_CODES):
        return False, ""
    # Never surface provider stderr; it can include identifiers or credentials.
    raise AcceptanceError("OCI request failed without an authorization-denial response")


def verify_ids(source: dict[str, str]) -> tuple[str, ...]:
    ids = tuple(source.get(key, "").strip() for key in BUNDLES)
    if any(not OCID.fullmatch(item) for item in ids) or len(set(ids)) != 4:
        raise AcceptanceError("four distinct authorized Vault secret OCIDs are required")
    return ids


def evaluate(
    mode: str,
    secret_ids: tuple[str, ...],
    *,
    forbidden_id: str | None,
    run: Callable = command,
) -> dict[str, object]:
    if mode not in ("deny-four", "allow-four"):
        raise AcceptanceError("invalid probe mode")
    if len(secret_ids) != 4 or len(set(secret_ids)) != 4:
        raise AcceptanceError("exactly four distinct secret IDs required")
    if mode == "allow-four":
        if not forbidden_id or not OCID.fullmatch(forbidden_id):
            raise AcceptanceError("a known-existing fifth forbidden secret is required")
        if forbidden_id in secret_ids:
            raise AcceptanceError("forbidden fixture must be outside the authorized four")
    for secret_id in secret_ids:
        allowed, returned_id = run([
            "secrets", "secret-bundle", "get", "--secret-id", secret_id,
            "--query", 'data."secret-id"', "--raw-output",
        ])
        if mode == "deny-four" and allowed:
            raise AcceptanceError("untagged identity unexpectedly accessed a Vault bundle")
        if mode == "allow-four" and (not allowed or returned_id != secret_id):
            raise AcceptanceError("tagged identity could not verify an authorized Vault bundle")
    if mode == "allow-four":
        allowed, _ = run([
            "secrets", "secret-bundle", "get", "--secret-id", forbidden_id,
            "--query", 'data."secret-id"', "--raw-output",
        ])
        if allowed:
            raise AcceptanceError("tagged identity unexpectedly accessed forbidden secret")
    return {
        "status": "UNTAGGED_FOUR_DENIED" if mode == "deny-four" else "TAGGED_FOUR_ALLOWED_FIFTH_DENIED",
        "instance_principal": True,
        "secret_contents_displayed": False,
        "secret_access_checks": 4 if mode == "deny-four" else 5,
        "full_recovery_verified": False,
    }


def verify_revocation_with_bounded_retries(
    secret_ids: tuple[str, ...],
    *,
    max_propagation_seconds: int = 600,
    run: Callable | None = None,
    clock: Callable = time.monotonic,
    sleep: Callable = time.sleep,
) -> dict[str, object]:
    """Require all four real denials after tag removal; do not retry network faults."""
    if not 1 <= max_propagation_seconds <= 600:
        raise AcceptanceError("revocation propagation window must be 1..600 seconds")
    deadline = clock() + max_propagation_seconds

    def bounded_request(argv: list[str]) -> tuple[bool, str]:
        remaining = deadline - clock()
        if remaining <= 0:
            raise AcceptanceError("revocation propagation timed out")
        # Bound every real subprocess by the remaining shared deadline.
        result = (
            command(argv, timeout=min(60.0, remaining))
            if run is None else run(argv)
        )
        # Slow denied responses must not report success after the cutoff.
        if clock() >= deadline:
            raise AcceptanceError("revocation propagation timed out")
        return result

    while True:
        try:
            evidence = evaluate(
                "deny-four", secret_ids, forbidden_id=None, run=bounded_request
            )
        except AcceptanceError as exc:
            # Only a still-effective ALLOW decision can be attributed to IAM
            # propagation. Transport and malformed responses fail immediately.
            if str(exc) != "untagged identity unexpectedly accessed a Vault bundle":
                raise
            remaining = deadline - clock()
            if remaining <= 0:
                raise AcceptanceError(
                    "revocation propagation timed out: Vault access still permitted"
                ) from exc
            sleep(min(10.0, remaining))
            continue
        return {
            **evidence,
            "status": "POST_REVOCATION_FOUR_DENIED",
            "effective_vault_access_denied": True,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("deny-four", "allow-four", "verify-revoked"))
    parser.add_argument("--expected-instance-id", required=True)
    parser.add_argument("--forbidden-secret-id")
    parser.add_argument("--approve-live-vault-reads", action="store_true")
    parser.add_argument("--max-propagation-seconds", type=int, default=600)
    args = parser.parse_args(argv)
    try:
        if not args.approve_live_vault_reads:
            raise AcceptanceError("separate explicit approval for live Vault reads required")
        if not args.expected_instance_id.startswith("ocid1.instance."):
            raise AcceptanceError("exact expected candidate instance OCID required")
        if identity() != args.expected_instance_id:
            raise AcceptanceError("instance metadata does not match approved candidate")
        secret_ids = verify_ids(os.environ)
        if args.mode == "verify-revoked":
            result = verify_revocation_with_bounded_retries(
                secret_ids,
                max_propagation_seconds=args.max_propagation_seconds,
            )
        else:
            result = evaluate(
                args.mode, secret_ids, forbidden_id=args.forbidden_secret_id
            )
    except AcceptanceError as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
