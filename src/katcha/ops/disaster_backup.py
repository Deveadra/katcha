from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from katcha.runtime_fence import assert_mutation_authority


class DisasterBackupError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class R2BackupConfig:
    endpoint_url: str
    access_key: str
    secret_key: str
    session_token: str | None
    bucket: str
    prefix: str
    region: str
    force_path_style: bool

    @classmethod
    def from_env(cls, purpose: Literal["backup", "restore"]) -> "R2BackupConfig":
        stem = "KATCHA_BACKUP_R2" if purpose == "backup" else "KATCHA_RESTORE_R2"

        def required(name: str) -> str:
            value = os.environ.get(f"{stem}_{name}", "").strip()
            if not value:
                raise DisasterBackupError(
                    f"missing required {purpose} setting: {stem}_{name}"
                )
            return value

        endpoint = required("ENDPOINT_URL")
        if not endpoint.startswith("https://"):
            raise DisasterBackupError(f"{stem}_ENDPOINT_URL must use HTTPS")

        prefix = os.environ.get(f"{stem}_PREFIX", "postgres").strip().strip("/")
        if not prefix:
            raise DisasterBackupError(f"{stem}_PREFIX cannot be empty")

        session_token = os.environ.get(f"{stem}_SESSION_TOKEN", "").strip() or None
        force_path_style = (
            os.environ.get(f"{stem}_FORCE_PATH_STYLE", "false").strip().casefold()
            == "true"
        )
        return cls(
            endpoint_url=endpoint,
            access_key=required("ACCESS_KEY"),
            secret_key=required("SECRET_KEY"),
            session_token=session_token,
            bucket=required("BUCKET"),
            prefix=prefix,
            region=os.environ.get(f"{stem}_REGION", "auto").strip() or "auto",
            force_path_style=force_path_style,
        )

    def client(self):
        kwargs: dict[str, Any] = {
            "endpoint_url": self.endpoint_url,
            "region_name": self.region,
            "aws_access_key_id": self.access_key,
            "aws_secret_access_key": self.secret_key,
            "config": Config(
                s3={"addressing_style": "path" if self.force_path_style else "auto"}
            ),
        }
        if self.session_token:
            kwargs["aws_session_token"] = self.session_token
        return boto3.client("s3", **kwargs)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_mapping(path: Path) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) != 2:
            raise DisasterBackupError(
                f"invalid database mapping at line {number}: expected filename<TAB>database"
            )
        filename, database = (part.strip() for part in parts)
        if (
            not filename
            or not database
            or "/" in filename
            or "\\" in filename
            or filename in {".", ".."}
        ):
            raise DisasterBackupError(f"invalid database mapping at line {number}")
        rows.append((filename, database))
    if not rows:
        raise DisasterBackupError("database mapping is empty")
    if len({filename for filename, _ in rows}) != len(rows):
        raise DisasterBackupError("database mapping contains duplicate filenames")
    if len({database for _, database in rows}) != len(rows):
        raise DisasterBackupError("database mapping contains duplicate database names")
    return rows


def create_manifest(
    *,
    directory: Path,
    mapping_path: Path,
    backup_id: str,
    release_sha: str,
    deployment_id: str,
    deployment_epoch: int,
) -> Path:
    if not backup_id or "/" in backup_id or "\\" in backup_id:
        raise DisasterBackupError("backup_id must be a single path-safe component")
    if len(release_sha) != 40 or any(ch not in "0123456789abcdef" for ch in release_sha):
        raise DisasterBackupError("release_sha must be an exact lowercase git SHA")
    if deployment_epoch < 1:
        raise DisasterBackupError("deployment_epoch must be positive")

    rows = _read_mapping(mapping_path)
    databases: list[dict[str, object]] = []
    for filename, database in rows:
        dump_path = directory / filename
        if not dump_path.is_file():
            raise DisasterBackupError(f"database dump is missing: {filename}")
        size_bytes = dump_path.stat().st_size
        if size_bytes <= 0:
            raise DisasterBackupError(f"database dump is empty: {filename}")
        databases.append(
            {
                "database": database,
                "file": filename,
                "sha256": _sha256(dump_path),
                "size_bytes": size_bytes,
            }
        )

    manifest = {
        "format_version": 1,
        "backup_id": backup_id,
        "created_at": datetime.now(UTC).isoformat(),
        "release_sha": release_sha,
        "deployment_id": deployment_id,
        "deployment_epoch": deployment_epoch,
        "database_count": len(databases),
        "databases": databases,
    }
    path = directory / "manifest.json"
    path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DisasterBackupError(f"invalid backup manifest: {path}") from exc
    if not isinstance(data, dict) or data.get("format_version") != 1:
        raise DisasterBackupError("unsupported backup manifest format")
    databases = data.get("databases")
    if not isinstance(databases, list) or not databases:
        raise DisasterBackupError("backup manifest has no databases")
    if int(data.get("database_count") or 0) != len(databases):
        raise DisasterBackupError("backup manifest database count is inconsistent")
    return data


def verify_directory(directory: Path, manifest: dict[str, Any]) -> None:
    for row in manifest["databases"]:
        if not isinstance(row, dict):
            raise DisasterBackupError("backup manifest contains an invalid database row")
        filename = str(row.get("file") or "")
        expected_sha = str(row.get("sha256") or "")
        expected_size = int(row.get("size_bytes") or 0)
        if not filename or "/" in filename or "\\" in filename:
            raise DisasterBackupError("backup manifest contains an unsafe filename")
        path = directory / filename
        if not path.is_file():
            raise DisasterBackupError(f"backup file is missing: {filename}")
        if path.stat().st_size != expected_size:
            raise DisasterBackupError(f"backup file size mismatch: {filename}")
        if _sha256(path) != expected_sha:
            raise DisasterBackupError(f"backup file checksum mismatch: {filename}")


def _not_found(exc: ClientError) -> bool:
    code = str(exc.response.get("Error", {}).get("Code") or "")
    status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") or 0)
    return code in {"404", "NoSuchKey", "NotFound"} or status == 404


def _object_exists(client, bucket: str, key: str) -> bool:
    try:
        client.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as exc:
        if _not_found(exc):
            return False
        raise


def upload_backup(directory: Path, config: R2BackupConfig) -> str:
    manifest_path = directory / "manifest.json"
    manifest = load_manifest(manifest_path)
    verify_directory(directory, manifest)
    assert_mutation_authority("backup.r2_upload")

    backup_id = str(manifest["backup_id"])
    base = f"{config.prefix}/{backup_id}"
    client = config.client()

    manifest_bytes = manifest_path.read_bytes()
    complete_key = f"{base}/_COMPLETE.json"
    keys = [
        *(f"{base}/{row['file']}" for row in manifest["databases"]),
        f"{base}/manifest.json",
        complete_key,
    ]
    for key in keys:
        if _object_exists(client, config.bucket, key):
            raise DisasterBackupError(
                f"refusing to overwrite an existing immutable backup object: {key}"
            )

    for row in manifest["databases"]:
        filename = str(row["file"])
        client.upload_file(
            str(directory / filename),
            config.bucket,
            f"{base}/{filename}",
            ExtraArgs={"ContentType": "application/octet-stream"},
        )

    client.put_object(
        Bucket=config.bucket,
        Key=f"{base}/manifest.json",
        Body=manifest_bytes,
        ContentType="application/json",
    )
    complete = {
        "format_version": 1,
        "backup_id": backup_id,
        "manifest_key": f"{base}/manifest.json",
        "manifest_sha256": _manifest_sha256(manifest_bytes),
        "completed_at": datetime.now(UTC).isoformat(),
    }
    client.put_object(
        Bucket=config.bucket,
        Key=complete_key,
        Body=(json.dumps(complete, sort_keys=True) + "\n").encode(),
        ContentType="application/json",
    )
    return complete_key


def _list_completion_keys(client, config: R2BackupConfig) -> list[str]:
    prefix = f"{config.prefix}/"
    token: str | None = None
    keys: list[str] = []
    while True:
        kwargs: dict[str, object] = {
            "Bucket": config.bucket,
            "Prefix": prefix,
            "MaxKeys": 1000,
        }
        if token:
            kwargs["ContinuationToken"] = token
        response = client.list_objects_v2(**kwargs)
        for row in response.get("Contents") or []:
            key = str(row.get("Key") or "")
            if key.endswith("/_COMPLETE.json"):
                keys.append(key)
        if not response.get("IsTruncated"):
            return sorted(keys)
        token = str(response.get("NextContinuationToken") or "")
        if not token:
            raise DisasterBackupError("R2 listing was truncated without a continuation token")


def latest_completion_key(client, config: R2BackupConfig) -> str:
    keys = _list_completion_keys(client, config)
    if not keys:
        raise DisasterBackupError("no completed PostgreSQL backups were found")
    return keys[-1]


def _get_json(client, bucket: str, key: str) -> tuple[dict[str, Any], bytes]:
    response = client.get_object(Bucket=bucket, Key=key)
    body = response["Body"]
    try:
        raw = body.read()
    finally:
        body.close()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DisasterBackupError(f"backup object is not valid JSON: {key}") from exc
    if not isinstance(data, dict):
        raise DisasterBackupError(f"backup object is not a JSON object: {key}")
    return data, raw


def download_latest(destination: Path, config: R2BackupConfig) -> Path:
    client = config.client()
    complete_key = latest_completion_key(client, config)
    complete, _ = _get_json(client, config.bucket, complete_key)
    manifest_key = str(complete.get("manifest_key") or "")
    expected_manifest_sha = str(complete.get("manifest_sha256") or "")
    if not manifest_key.startswith(f"{config.prefix}/") or not expected_manifest_sha:
        raise DisasterBackupError("completion marker does not reference a valid manifest")

    manifest, manifest_bytes = _get_json(client, config.bucket, manifest_key)
    if _manifest_sha256(manifest_bytes) != expected_manifest_sha:
        raise DisasterBackupError("downloaded manifest checksum does not match completion marker")
    backup_id = str(manifest.get("backup_id") or "")
    expected_prefix = f"{config.prefix}/{backup_id}/"
    if manifest_key != f"{expected_prefix}manifest.json":
        raise DisasterBackupError("completion marker and manifest backup IDs disagree")

    target = destination / backup_id
    target.mkdir(parents=True, exist_ok=False)
    (target / "manifest.json").write_bytes(manifest_bytes)

    for row in manifest.get("databases") or []:
        if not isinstance(row, dict):
            raise DisasterBackupError("manifest database entry is invalid")
        filename = str(row.get("file") or "")
        if not filename or "/" in filename or "\\" in filename:
            raise DisasterBackupError("manifest contains an unsafe backup filename")
        client.download_file(
            config.bucket,
            f"{expected_prefix}{filename}",
            str(target / filename),
        )

    verify_directory(target, load_manifest(target / "manifest.json"))
    return target


def _cmd_manifest(args: argparse.Namespace) -> int:
    path = create_manifest(
        directory=args.directory,
        mapping_path=args.mapping,
        backup_id=args.backup_id,
        release_sha=args.release_sha,
        deployment_id=args.deployment_id,
        deployment_epoch=args.deployment_epoch,
    )
    print(path)
    return 0


def _cmd_upload(args: argparse.Namespace) -> int:
    print(upload_backup(args.directory, R2BackupConfig.from_env("backup")))
    return 0


def _cmd_download(args: argparse.Namespace) -> int:
    print(download_latest(args.destination, R2BackupConfig.from_env("restore")))
    return 0


def _cmd_restore_plan(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.directory / "manifest.json")
    verify_directory(args.directory, manifest)
    for row in manifest["databases"]:
        print(f"{row['file']}\t{row['database']}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    manifest = subparsers.add_parser("manifest")
    manifest.add_argument("--directory", type=Path, required=True)
    manifest.add_argument("--mapping", type=Path, required=True)
    manifest.add_argument("--backup-id", required=True)
    manifest.add_argument("--release-sha", required=True)
    manifest.add_argument("--deployment-id", required=True)
    manifest.add_argument("--deployment-epoch", type=int, required=True)
    manifest.set_defaults(func=_cmd_manifest)

    upload = subparsers.add_parser("upload")
    upload.add_argument("--directory", type=Path, required=True)
    upload.set_defaults(func=_cmd_upload)

    download = subparsers.add_parser("download-latest")
    download.add_argument("--destination", type=Path, required=True)
    download.set_defaults(func=_cmd_download)

    restore_plan = subparsers.add_parser("restore-plan")
    restore_plan.add_argument("--directory", type=Path, required=True)
    restore_plan.set_defaults(func=_cmd_restore_plan)

    args = parser.parse_args()
    try:
        return int(args.func(args))
    except DisasterBackupError as exc:
        print(f"BACKUP_ERROR: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
