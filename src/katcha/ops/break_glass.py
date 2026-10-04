from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import secrets
import stat
import tarfile
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


class BreakGlassError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BreakGlassR2Config:
    endpoint_url: str
    access_key: str
    secret_key: str
    bucket: str
    prefix: str
    region: str
    force_path_style: bool

    @classmethod
    def from_env(cls) -> "BreakGlassR2Config":
        def required(name: str) -> str:
            value = os.environ.get(f"KATCHA_BREAK_GLASS_R2_{name}", "").strip()
            if not value:
                raise BreakGlassError(
                    f"missing required break-glass setting: "
                    f"KATCHA_BREAK_GLASS_R2_{name}"
                )
            return value

        endpoint = required("ENDPOINT_URL").rstrip("/")
        if not endpoint.startswith("https://"):
            raise BreakGlassError(
                "KATCHA_BREAK_GLASS_R2_ENDPOINT_URL must use HTTPS"
            )
        prefix = os.environ.get(
            "KATCHA_BREAK_GLASS_R2_PREFIX",
            "bootstrap-handoff",
        ).strip().strip("/")
        if not prefix:
            raise BreakGlassError("break-glass R2 prefix cannot be empty")
        return cls(
            endpoint_url=endpoint,
            access_key=required("ACCESS_KEY"),
            secret_key=required("SECRET_KEY"),
            bucket=required("BUCKET"),
            prefix=prefix,
            region=os.environ.get(
                "KATCHA_BREAK_GLASS_R2_REGION",
                "auto",
            ).strip()
            or "auto",
            force_path_style=(
                os.environ.get(
                    "KATCHA_BREAK_GLASS_R2_FORCE_PATH_STYLE",
                    "false",
                )
                .strip()
                .casefold()
                == "true"
            ),
        )

    def client(self):
        import boto3
        from botocore.config import Config

        return boto3.client(
            "s3",
            endpoint_url=self.endpoint_url,
            region_name=self.region,
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
            config=Config(
                signature_version="s3v4",
                s3={
                    "addressing_style": (
                        "path" if self.force_path_style else "auto"
                    )
                },
            ),
        )


_REQUIRED_FILES = (
    "katcha.env",
    "backup.env",
    "restore.env",
    "aws_bundle.tgz",
)
_MAX_HANDOFF_BYTES = 4 * 1024 * 1024


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _read_required_file(path: Path, label: str) -> bytes:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise BreakGlassError(f"cannot read {label}: {path}") from exc
    if not data:
        raise BreakGlassError(f"{label} cannot be empty")
    return data


def build_bundle(
    *,
    production_env: Path,
    backup_env: Path,
    restore_env: Path,
    aws_bundle: Path,
    ttl_seconds: int,
    now: datetime | None = None,
) -> bytes:
    if ttl_seconds < 300 or ttl_seconds > 3600:
        raise BreakGlassError(
            "break-glass handoff TTL must be between 300 and 3600 seconds"
        )
    current = (now or _utc_now()).astimezone(UTC)
    sources = {
        "katcha.env": production_env,
        "backup.env": backup_env,
        "restore.env": restore_env,
        "aws_bundle.tgz": aws_bundle,
    }
    files: dict[str, dict[str, str | int]] = {}
    total = 0
    for name, path in sources.items():
        data = _read_required_file(path, name)
        total += len(data)
        files[name] = {
            "base64": base64.b64encode(data).decode("ascii"),
            "sha256": _sha256(data),
            "size_bytes": len(data),
        }
    if total > _MAX_HANDOFF_BYTES:
        raise BreakGlassError(
            f"break-glass handoff content is too large: {total} bytes"
        )
    payload = {
        "format_version": 1,
        "handoff_id": str(uuid.uuid4()),
        "created_at": current.isoformat(),
        "expires_at": (current + timedelta(seconds=ttl_seconds)).isoformat(),
        "files": files,
    }
    return (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")


def encrypt_bundle(bundle: bytes) -> tuple[str, bytes]:
    key = Fernet.generate_key()
    encrypted = Fernet(key).encrypt(bundle)
    return key.decode("ascii"), encrypted


def _parse_bundle(
    encrypted: bytes,
    fernet_key: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    try:
        plaintext = Fernet(fernet_key.encode("ascii")).decrypt(encrypted)
    except (ValueError, InvalidToken, UnicodeEncodeError) as exc:
        raise BreakGlassError(
            "break-glass handoff could not be decrypted"
        ) from exc
    try:
        payload = json.loads(plaintext)
    except json.JSONDecodeError as exc:
        raise BreakGlassError("break-glass handoff is not valid JSON") from exc
    if not isinstance(payload, dict) or payload.get("format_version") != 1:
        raise BreakGlassError("unsupported break-glass handoff format")
    expires_raw = str(payload.get("expires_at") or "")
    try:
        expires_at = datetime.fromisoformat(
            expires_raw.replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise BreakGlassError(
            "break-glass handoff has an invalid expiry"
        ) from exc
    if expires_at.tzinfo is None:
        raise BreakGlassError(
            "break-glass handoff expiry must include a timezone"
        )
    current = (now or _utc_now()).astimezone(UTC)
    if current >= expires_at.astimezone(UTC):
        raise BreakGlassError("break-glass handoff has expired")
    files = payload.get("files")
    if not isinstance(files, dict) or set(files) != set(_REQUIRED_FILES):
        raise BreakGlassError(
            "break-glass handoff does not contain the exact required files"
        )
    return payload


def _decode_file(payload: dict[str, Any], name: str) -> bytes:
    files = payload["files"]
    row = files.get(name)
    if not isinstance(row, dict):
        raise BreakGlassError(f"break-glass file metadata is invalid: {name}")
    raw = str(row.get("base64") or "")
    try:
        data = base64.b64decode(raw, validate=True)
    except Exception as exc:
        raise BreakGlassError(
            f"break-glass file is not valid base64: {name}"
        ) from exc
    expected_size = int(row.get("size_bytes") or -1)
    expected_sha = str(row.get("sha256") or "")
    if len(data) != expected_size or _sha256(data) != expected_sha:
        raise BreakGlassError(
            f"break-glass file failed integrity validation: {name}"
        )
    return data


def _safe_install_aws_bundle(data: bytes, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    destination.chmod(0o700)
    try:
        archive = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
    except tarfile.TarError as exc:
        raise BreakGlassError("AWS break-glass bundle is not a valid tar.gz") from exc
    with archive:
        members = archive.getmembers()
        if not members:
            raise BreakGlassError("AWS break-glass bundle is empty")
        for member in members:
            member_path = Path(member.name)
            if (
                member_path.is_absolute()
                or ".." in member_path.parts
                or member.issym()
                or member.islnk()
                or member.isdev()
                or member.isfifo()
            ):
                raise BreakGlassError(
                    f"unsafe AWS break-glass bundle member: {member.name}"
                )
            if not (member.isdir() or member.isfile()):
                raise BreakGlassError(
                    f"unsupported AWS break-glass bundle member: {member.name}"
                )
        for member in members:
            target = destination / member.name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                target.chmod(0o700)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise BreakGlassError(
                    f"cannot read AWS break-glass bundle member: {member.name}"
                )
            with source, target.open("wb") as handle:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
            target.chmod(0o600)


def install_bundle(
    encrypted: bytes,
    *,
    fernet_key: str,
    root: Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    payload = _parse_bundle(encrypted, fernet_key, now=now)
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    for name in ("katcha.env", "backup.env", "restore.env"):
        path = root / name
        path.write_bytes(_decode_file(payload, name))
        path.chmod(0o600)
    _safe_install_aws_bundle(
        _decode_file(payload, "aws_bundle.tgz"),
        root / "aws",
    )
    return {
        "handoff_id": payload["handoff_id"],
        "expires_at": payload["expires_at"],
    }


def download_encrypted(url: str) -> bytes:
    if not url.startswith("https://"):
        raise BreakGlassError("break-glass handoff URL must use HTTPS")
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Katcha-Break-Glass/1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read(_MAX_HANDOFF_BYTES + 1024 * 1024)
    except Exception as exc:
        raise BreakGlassError(
            "could not download break-glass handoff"
        ) from exc
    if not data or len(data) > _MAX_HANDOFF_BYTES + 1024 * 1024:
        raise BreakGlassError(
            "downloaded break-glass handoff is empty or unexpectedly large"
        )
    return data


def create_handoff(
    *,
    production_env: Path,
    backup_env: Path,
    restore_env: Path,
    aws_bundle: Path,
    ttl_seconds: int,
    config: BreakGlassR2Config,
) -> dict[str, str | int]:
    bundle = build_bundle(
        production_env=production_env,
        backup_env=backup_env,
        restore_env=restore_env,
        aws_bundle=aws_bundle,
        ttl_seconds=ttl_seconds,
    )
    fernet_key, encrypted = encrypt_bundle(bundle)
    object_key = (
        f"{config.prefix}/"
        f"{_utc_now().strftime('%Y%m%dT%H%M%SZ')}-"
        f"{secrets.token_hex(16)}.bin"
    )
    client = config.client()
    client.put_object(
        Bucket=config.bucket,
        Key=object_key,
        Body=encrypted,
        ContentType="application/octet-stream",
        CacheControl="no-store",
    )
    url = client.generate_presigned_url(
        "get_object",
        Params={"Bucket": config.bucket, "Key": object_key},
        ExpiresIn=ttl_seconds,
    )
    return {
        "format_version": 1,
        "url": url,
        "fernet_key": fernet_key,
        "object_key": object_key,
        "ttl_seconds": ttl_seconds,
        "ciphertext_sha256": _sha256(encrypted),
    }


def delete_handoff(object_key: str, config: BreakGlassR2Config) -> None:
    expected_prefix = f"{config.prefix}/"
    if not object_key.startswith(expected_prefix) or ".." in Path(object_key).parts:
        raise BreakGlassError(
            "refusing to delete object outside break-glass handoff prefix"
        )
    config.client().delete_object(Bucket=config.bucket, Key=object_key)


def _cmd_create(args: argparse.Namespace) -> int:
    result = create_handoff(
        production_env=args.production_env,
        backup_env=args.backup_env,
        restore_env=args.restore_env,
        aws_bundle=args.aws_bundle,
        ttl_seconds=args.ttl_seconds,
        config=BreakGlassR2Config.from_env(),
    )
    print(json.dumps(result, sort_keys=True))
    return 0


def _cmd_install(args: argparse.Namespace) -> int:
    encrypted = download_encrypted(args.url)
    result = install_bundle(
        encrypted,
        fernet_key=args.fernet_key,
        root=args.root,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


def _cmd_delete(args: argparse.Namespace) -> int:
    delete_handoff(args.object_key, BreakGlassR2Config.from_env())
    print(json.dumps({"deleted": args.object_key}, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create-handoff")
    create.add_argument("--production-env", type=Path, required=True)
    create.add_argument("--backup-env", type=Path, required=True)
    create.add_argument("--restore-env", type=Path, required=True)
    create.add_argument("--aws-bundle", type=Path, required=True)
    create.add_argument("--ttl-seconds", type=int, default=1800)
    create.set_defaults(func=_cmd_create)

    install = subparsers.add_parser("install-handoff")
    install.add_argument("--url", required=True)
    install.add_argument("--fernet-key", required=True)
    install.add_argument("--root", type=Path, required=True)
    install.set_defaults(func=_cmd_install)

    delete = subparsers.add_parser("delete-handoff")
    delete.add_argument("--object-key", required=True)
    delete.set_defaults(func=_cmd_delete)

    args = parser.parse_args()
    try:
        return int(args.func(args))
    except BreakGlassError as exc:
        print(f"BREAK_GLASS_ERROR: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
