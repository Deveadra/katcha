from __future__ import annotations

import argparse
from pathlib import Path
from urllib.parse import urlsplit

from katcha.ops.production_runtime import is_placeholder, read_env


def _required(values: dict[str, str], key: str, errors: list[str]) -> str:
    value = values.get(key, "").strip()
    if is_placeholder(value):
        errors.append(f"{key} is missing or still a placeholder")
    return value


def validate(
    production: dict[str, str],
    backup: dict[str, str],
    restore: dict[str, str],
) -> list[str]:
    errors: list[str] = []

    media_bucket = _required(production, "KATCHA_S3_BUCKET", errors)
    media_key = _required(production, "KATCHA_S3_ACCESS_KEY", errors)

    backup_endpoint = _required(backup, "KATCHA_BACKUP_R2_ENDPOINT_URL", errors)
    backup_key = _required(backup, "KATCHA_BACKUP_R2_ACCESS_KEY", errors)
    _required(backup, "KATCHA_BACKUP_R2_SECRET_KEY", errors)
    backup_bucket = _required(backup, "KATCHA_BACKUP_R2_BUCKET", errors)
    backup_prefix = _required(backup, "KATCHA_BACKUP_R2_PREFIX", errors)

    restore_endpoint = _required(restore, "KATCHA_RESTORE_R2_ENDPOINT_URL", errors)
    restore_key = _required(restore, "KATCHA_RESTORE_R2_ACCESS_KEY", errors)
    _required(restore, "KATCHA_RESTORE_R2_SECRET_KEY", errors)
    restore_bucket = _required(restore, "KATCHA_RESTORE_R2_BUCKET", errors)
    restore_prefix = _required(restore, "KATCHA_RESTORE_R2_PREFIX", errors)

    for key, endpoint in (
        ("KATCHA_BACKUP_R2_ENDPOINT_URL", backup_endpoint),
        ("KATCHA_RESTORE_R2_ENDPOINT_URL", restore_endpoint),
    ):
        if endpoint:
            parsed = urlsplit(endpoint)
            if parsed.scheme != "https":
                errors.append(f"{key} must use HTTPS")
            if not (parsed.hostname or "").endswith(".r2.cloudflarestorage.com"):
                errors.append(f"{key} must target Cloudflare R2")

    if backup_bucket and media_bucket and backup_bucket == media_bucket:
        errors.append("disaster backups must use a bucket separate from runtime media")
    if restore_bucket and backup_bucket and restore_bucket != backup_bucket:
        errors.append("restore credentials must target the same disaster-backup bucket")
    if restore_prefix and backup_prefix and restore_prefix.strip("/") != backup_prefix.strip("/"):
        errors.append("backup and restore credentials must use the same backup prefix")

    if backup_key and media_key and backup_key == media_key:
        errors.append("backup writer credentials must differ from runtime media credentials")
    if restore_key and media_key and restore_key == media_key:
        errors.append("restore reader credentials must differ from runtime media credentials")
    if restore_key and backup_key and restore_key == backup_key:
        errors.append("backup writer and restore reader credentials must be distinct")

    if backup.get("KATCHA_BACKUP_R2_REGION", "auto").strip() != "auto":
        errors.append("KATCHA_BACKUP_R2_REGION must be auto for Cloudflare R2")
    if restore.get("KATCHA_RESTORE_R2_REGION", "auto").strip() != "auto":
        errors.append("KATCHA_RESTORE_R2_REGION must be auto for Cloudflare R2")
    if backup.get("KATCHA_BACKUP_R2_FORCE_PATH_STYLE", "false").casefold() != "false":
        errors.append("KATCHA_BACKUP_R2_FORCE_PATH_STYLE must be false for R2")
    if restore.get("KATCHA_RESTORE_R2_FORCE_PATH_STYLE", "false").casefold() != "false":
        errors.append("KATCHA_RESTORE_R2_FORCE_PATH_STYLE must be false for R2")

    return list(dict.fromkeys(errors))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--production-env",
        type=Path,
        default=Path("/etc/katcha/katcha.env"),
    )
    parser.add_argument(
        "--backup-env",
        type=Path,
        default=Path("/etc/katcha/backup.env"),
    )
    parser.add_argument(
        "--restore-env",
        type=Path,
        default=Path("/etc/katcha/restore.env"),
    )
    args = parser.parse_args()

    for path in (args.production_env, args.backup_env, args.restore_env):
        if not path.is_file():
            print(f"ERROR: disaster recovery environment file does not exist: {path}")
            return 2

    errors = validate(
        read_env(args.production_env),
        read_env(args.backup_env),
        read_env(args.restore_env),
    )
    if errors:
        print("Katcha disaster recovery configuration validation failed:")
        for error in errors:
            print(f"- {error}")
        return 2

    print("Katcha disaster recovery credential separation passed safety checks.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
