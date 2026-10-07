"""Production runtime configuration validation for hosted Katcha."""

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


    if values.get("KATCHA_LEADERSHIP_FENCE_MODE", "").casefold() != "http":
        errors.append("KATCHA_LEADERSHIP_FENCE_MODE must be http in hosted production")

    fence_url = require(values, "KATCHA_LEADERSHIP_FENCE_URL", errors)
    if fence_url:
        parsed = urlsplit(fence_url)
        if parsed.scheme != "https" or not parsed.hostname:
            errors.append("KATCHA_LEADERSHIP_FENCE_URL must be a public HTTPS coordinator endpoint")

    fence_token = require(values, "KATCHA_LEADERSHIP_FENCE_TOKEN", errors)
    if fence_token and not is_placeholder(fence_token) and len(fence_token) < 32:
        errors.append("KATCHA_LEADERSHIP_FENCE_TOKEN must contain at least 32 characters")

    deployment_id = require(values, "KATCHA_DEPLOYMENT_ID", errors)
    if deployment_id and deployment_id.casefold() in {"local", "development", "dev"}:
        errors.append("KATCHA_DEPLOYMENT_ID must identify the hosted deployment")

    require(values, "KATCHA_CLOUDFLARE_TUNNEL_TOKEN", errors)

    raw_epoch = require(values, "KATCHA_DEPLOYMENT_EPOCH", errors)
    if raw_epoch and not is_placeholder(raw_epoch):
        try:
            epoch = int(raw_epoch)
        except ValueError:
            errors.append("KATCHA_DEPLOYMENT_EPOCH must be an integer")
        else:
            if epoch < 1:
                errors.append("KATCHA_DEPLOYMENT_EPOCH must be at least 1")

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

    s3_access_key = require(values, "KATCHA_S3_ACCESS_KEY", errors)
    s3_secret_key = require(values, "KATCHA_S3_SECRET_KEY", errors)
    s3_bucket = require(values, "KATCHA_S3_BUCKET", errors)
    require(values, "KATCHA_CREDENTIAL_ENCRYPTION_KEY", errors)

    if s3_access_key and not is_placeholder(s3_access_key) and len(s3_access_key) != 32:
        errors.append("KATCHA_S3_ACCESS_KEY must be a 32-character Cloudflare R2 access key")
    if s3_secret_key and not is_placeholder(s3_secret_key) and len(s3_secret_key) != 64:
        errors.append("KATCHA_S3_SECRET_KEY must be a 64-character Cloudflare R2 secret key")
    if s3_bucket and s3_bucket != "katcha-media-prod":
        errors.append("KATCHA_S3_BUCKET must be katcha-media-prod in hosted production")

    encryption_key = values.get("KATCHA_CREDENTIAL_ENCRYPTION_KEY", "")
    if not is_placeholder(encryption_key) and len(encryption_key) < 32:
        errors.append("KATCHA_CREDENTIAL_ENCRYPTION_KEY must contain at least 32 characters")

    control_token = values.get("KATCHA_CONTROL_API_TOKEN", "")
    principal_registry = values.get("KATCHA_CONTROL_PRINCIPALS", "").strip()
    principals_configured = (
        principal_registry not in {"", "[]", "{}"}
        and not is_placeholder(principal_registry)
    )
    if is_placeholder(control_token) and not principals_configured:
        errors.append(
            "configure KATCHA_CONTROL_API_TOKEN or a non-empty KATCHA_CONTROL_PRINCIPALS"
        )

    redirect = require(values, "KATCHA_YOUTUBE_REDIRECT_URI", errors)
    if redirect:
        parsed = urlsplit(redirect)
        if parsed.scheme != "https" or parsed.hostname in {"localhost", "127.0.0.1"}:
            errors.append("KATCHA_YOUTUBE_REDIRECT_URI must use the public HTTPS production host")

    if values.get("KATCHA_RENDER_BACKEND", "").casefold() != "lambda":
        errors.append("KATCHA_RENDER_BACKEND must be lambda on the low-memory control plane")

    if values.get("KATCHA_EXTERNAL_COMPUTE_ENABLED", "").casefold() != "true":
        errors.append(
            "KATCHA_EXTERNAL_COMPUTE_ENABLED must be true when Lambda rendering is enabled"
        )

    compute_url = require(values, "KATCHA_EXTERNAL_COMPUTE_COORDINATOR_URL", errors)
    if compute_url:
        parsed = urlsplit(compute_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.hostname in {"localhost", "127.0.0.1"}
        ):
            errors.append(
                "KATCHA_EXTERNAL_COMPUTE_COORDINATOR_URL must be a public HTTPS endpoint"
            )

    compute_token = require(values, "KATCHA_EXTERNAL_COMPUTE_TOKEN", errors)
    if (
        compute_token
        and not is_placeholder(compute_token)
        and len(compute_token) < 32
    ):
        errors.append(
            "KATCHA_EXTERNAL_COMPUTE_TOKEN must contain at least 32 characters"
        )

    render_ceiling = require(
        values,
        "KATCHA_REMOTION_LAMBDA_MAX_RENDER_COST_USD",
        errors,
    )
    if render_ceiling and not is_placeholder(render_ceiling):
        try:
            ceiling = float(render_ceiling)
        except ValueError:
            errors.append(
                "KATCHA_REMOTION_LAMBDA_MAX_RENDER_COST_USD must be a number"
            )
        else:
            if ceiling <= 0:
                errors.append(
                    "KATCHA_REMOTION_LAMBDA_MAX_RENDER_COST_USD must be positive"
                )

    raw_budget_ttl = values.get(
        "KATCHA_REMOTION_LAMBDA_BUDGET_TTL_SECONDS",
        "7200",
    ).strip()
    try:
        budget_ttl = int(raw_budget_ttl)
    except ValueError:
        errors.append(
            "KATCHA_REMOTION_LAMBDA_BUDGET_TTL_SECONDS must be an integer"
        )
    else:
        if budget_ttl < 300 or budget_ttl > 604800:
            errors.append(
                "KATCHA_REMOTION_LAMBDA_BUDGET_TTL_SECONDS must be between "
                "300 and 604800"
            )

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
