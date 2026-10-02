#!/usr/bin/env python3
"""Fail closed when a hosted Katcha environment still contains local/dev settings."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from urllib.parse import urlsplit

PLACEHOLDER = re.compile(r"(CHANGE_ME|CHANGEME|EXAMPLE|REPLACE_ME)", re.IGNORECASE)
SHA = re.compile(r"^[0-9a-f]{40}$")
AWS_ACCOUNT = re.compile(r"^[0-9]{12}$")


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key.strip()] = value
    return values


def is_placeholder(value: str) -> bool:
    return not value.strip() or bool(PLACEHOLDER.search(value))


def require(values: dict[str, str], key: str, errors: list[str]) -> str:
    value = values.get(key, "").strip()
    if is_placeholder(value):
        errors.append(f"{key} is missing or still a placeholder")
    return value


def validate(values: dict[str, str]) -> list[str]:
    errors: list[str] = []

    if values.get("KATCHA_ENV", "").casefold() != "production":
        errors.append("KATCHA_ENV must be production")

    release = require(values, "KATCHA_RELEASE_SHA", errors)
    if release and not SHA.fullmatch(release):
        errors.append("KATCHA_RELEASE_SHA must be an exact 40-character lowercase git SHA")

    postgres_password = require(values, "KATCHA_POSTGRES_PASSWORD", errors)
    if postgres_password.casefold() in {"katcha", "password", "postgres"}:
        errors.append("KATCHA_POSTGRES_PASSWORD uses an unsafe development/default value")

    database_url = require(values, "KATCHA_DATABASE_URL", errors)
    if database_url:
        parsed = urlsplit(database_url.replace("postgresql+psycopg://", "postgresql://", 1))
        if parsed.hostname != "postgres":
            errors.append("KATCHA_DATABASE_URL must use the production compose postgres service")
        if parsed.password in {None, "", "katcha", "password", "postgres"}:
            errors.append("KATCHA_DATABASE_URL contains a missing or unsafe database password")

    endpoint = require(values, "KATCHA_S3_ENDPOINT_URL", errors)
    if endpoint:
        parsed = urlsplit(endpoint)
        if parsed.scheme != "https":
            errors.append("KATCHA_S3_ENDPOINT_URL must use HTTPS")
        if not (parsed.hostname or "").endswith(".r2.cloudflarestorage.com"):
            errors.append("KATCHA_S3_ENDPOINT_URL must target Cloudflare R2 in this deployment")

    if values.get("KATCHA_S3_REGION", "") != "auto":
        errors.append("KATCHA_S3_REGION must be auto for Cloudflare R2")
    if values.get("KATCHA_S3_FORCE_PATH_STYLE", "").casefold() != "false":
        errors.append("KATCHA_S3_FORCE_PATH_STYLE must be false for the hosted R2 profile")

    for key in (
        "KATCHA_S3_ACCESS_KEY",
        "KATCHA_S3_SECRET_KEY",
        "KATCHA_S3_BUCKET",
        "KATCHA_CREDENTIAL_ENCRYPTION_KEY",
    ):
        require(values, key, errors)

    encryption_key = values.get("KATCHA_CREDENTIAL_ENCRYPTION_KEY", "")
    if not is_placeholder(encryption_key) and len(encryption_key) < 32:
        errors.append("KATCHA_CREDENTIAL_ENCRYPTION_KEY must contain at least 32 characters")

    if is_placeholder(values.get("KATCHA_CONTROL_API_TOKEN", "")) and is_placeholder(
        values.get("KATCHA_CONTROL_PRINCIPALS", "")
    ):
        errors.append("configure KATCHA_CONTROL_API_TOKEN or KATCHA_CONTROL_PRINCIPALS")

    redirect = require(values, "KATCHA_YOUTUBE_REDIRECT_URI", errors)
    if redirect:
        parsed = urlsplit(redirect)
        if parsed.scheme != "https" or parsed.hostname in {"localhost", "127.0.0.1"}:
            errors.append("KATCHA_YOUTUBE_REDIRECT_URI must use the public HTTPS production host")

    if values.get("KATCHA_RENDER_BACKEND", "").casefold() != "lambda":
        errors.append("KATCHA_RENDER_BACKEND must be lambda on the low-memory control plane")

    account = require(values, "KATCHA_AWS_EXPECTED_ACCOUNT_ID", errors)
    if account and not is_placeholder(account) and not AWS_ACCOUNT.fullmatch(account):
        errors.append("KATCHA_AWS_EXPECTED_ACCOUNT_ID must be a 12-digit AWS account ID")

    for key in (
        "KATCHA_AWS_PROFILE",
        "KATCHA_REMOTION_LAMBDA_FUNCTION_NAME",
        "KATCHA_REMOTION_LAMBDA_SERVE_URL",
        "KATCHA_REMOTION_STAGING_BUCKET",
    ):
        require(values, key, errors)

    return list(dict.fromkeys(errors))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, default=Path("/etc/katcha/katcha.env"))
    args = parser.parse_args()

    if not args.env_file.is_file():
        print(f"ERROR: production environment file does not exist: {args.env_file}")
        return 2

    errors = validate(read_env(args.env_file))
    if errors:
        print("Katcha production runtime validation failed:")
        for error in errors:
            print(f"- {error}")
        return 2

    print("Katcha production runtime configuration passed non-secret safety checks.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
