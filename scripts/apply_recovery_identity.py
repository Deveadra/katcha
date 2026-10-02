#!/usr/bin/env python3
"""Atomically install the deployment identity used by a recovered OCI host."""

from __future__ import annotations

import argparse
import os
import re
import stat
import tempfile
from datetime import UTC, datetime
from pathlib import Path

DEPLOYMENT_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
RELEASE_SHA = re.compile(r"^[0-9a-f]{40}$")


def _future_iso(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("paid fallback expiry must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("paid fallback expiry must include a timezone")
    if parsed <= datetime.now(UTC):
        raise ValueError("paid fallback expiry must be in the future")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def identity_values(args: argparse.Namespace) -> dict[str, str]:
    if not DEPLOYMENT_ID.fullmatch(args.deployment_id):
        raise ValueError("deployment ID is invalid")
    if args.deployment_epoch < 1:
        raise ValueError("deployment epoch must be positive")
    if not RELEASE_SHA.fullmatch(args.release_sha):
        raise ValueError("release SHA must be an exact lowercase 40-character git SHA")
    if args.cost_class not in {"always_free", "paid"}:
        raise ValueError("cost class must be always_free or paid")
    if args.recovery_incident_id and not DEPLOYMENT_ID.fullmatch(
        args.recovery_incident_id
    ):
        raise ValueError("recovery incident ID is invalid")

    paid_expires_at = ""
    if args.cost_class == "paid":
        if not args.paid_expires_at:
            raise ValueError("paid fallback requires an expiry")
        paid_expires_at = _future_iso(args.paid_expires_at)
    elif args.paid_expires_at:
        raise ValueError("Always Free deployment cannot carry a paid expiry")

    return {
        "KATCHA_RELEASE_SHA": args.release_sha,
        "KATCHA_DEPLOYMENT_ID": args.deployment_id,
        "KATCHA_DEPLOYMENT_EPOCH": str(args.deployment_epoch),
        "KATCHA_DEPLOYMENT_COST_CLASS": args.cost_class,
        "KATCHA_PAID_FALLBACK_EXPIRES_AT": paid_expires_at,
        "KATCHA_RECOVERY_INCIDENT_ID": args.recovery_incident_id or "",
    }


def update_env_file(path: Path, values: dict[str, str]) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"production environment file does not exist: {path}")

    original = path.read_text(encoding="utf-8")
    remaining = dict(values)
    output: list[str] = []
    for line in original.splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            output.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in remaining:
            output.append(f"{key}={remaining.pop(key)}")
        else:
            output.append(line)
    if output and output[-1] != "":
        output.append("")
    output.extend(f"{key}={value}" for key, value in remaining.items())
    output.append("")
    rendered = "\n".join(output)

    mode = stat.S_IMODE(path.stat().st_mode)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        handle.write(rendered)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    try:
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, default=Path("/etc/katcha/katcha.env"))
    parser.add_argument("--deployment-id", required=True)
    parser.add_argument("--deployment-epoch", required=True, type=int)
    parser.add_argument("--release-sha", required=True)
    parser.add_argument(
        "--cost-class",
        choices=("always_free", "paid"),
        default="always_free",
    )
    parser.add_argument("--paid-expires-at", default="")
    parser.add_argument("--recovery-incident-id", default="")
    args = parser.parse_args()

    try:
        values = identity_values(args)
        update_env_file(args.env_file, values)
    except (FileNotFoundError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 2

    print(
        "Installed Katcha deployment identity "
        f"{values['KATCHA_DEPLOYMENT_ID']}@{values['KATCHA_DEPLOYMENT_EPOCH']} "
        f"({values['KATCHA_DEPLOYMENT_COST_CLASS']})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
