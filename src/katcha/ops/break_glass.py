from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import secrets
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
    handoff_prefix: str
    escrow_prefix: str
    region: str
    force_path_style: bool

    @classmethod
    def from_env(cls) -> "BreakGlassR2Config":
        def required(name: str) -> str:
            value = os.environ.get(f"KATCHA_BREAK_GLASS_R2_{name}", "").strip()
            if not value:
                raise BreakGlassError(
                    "missing required break-glass setting: "
                    f"KATCHA_BREAK_GLASS_R2_{name}"
                )
            return value

        endpoint = required("ENDPOINT_URL").rstrip("/")
        if not endpoint.startswith("https://"):
            raise BreakGlassError(
                "KATCHA_BREAK_GLASS_R2_ENDPOINT_URL must use HTTPS"
            )
        handoff_prefix = os.environ.get(
            "KATCHA_BREAK_GLASS_R2_PREFIX",
            "bootstrap-handoff",
        ).strip().strip("/")
        escrow_prefix = os.environ.get(
            "KATCHA_BREAK_GLASS_ESCROW_PREFIX",
            "escrow",
        ).strip().strip("/")
        if not handoff_prefix:
            raise BreakGlassError("break-glass handoff prefix cannot be empty")
        if not escrow_prefix:
            raise BreakGlassError("break-glass escrow prefix cannot be empty")
        if handoff_prefix == escrow_prefix:
            raise BreakGlassError(
                "break-glass handoff and escrow prefixes must differ"
            )
        return cls(
            endpoint_url=endpoint,
            access_key=required("ACCESS_KEY"),
            secret_key=required("SECRET_KEY"),
            bucket=required("BUCKET"),
            handoff_prefix=handoff_prefix,
            escrow_prefix=escrow_prefix,
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

    @property
    def prefix(self) -> str:
        # Compatibility for the short-lived handoff lifecycle helper.
        return self.handoff_prefix

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
_MAX_RAW_BUNDLE_BYTES = 32 * 1024 * 1024
_MAX_ENCRYPTED_BYTES = 64 * 1024 * 1024


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


def _validated_aws_members(data: bytes) -> list[tarfile.TarInfo]:
    try:
        archive = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
    except tarfile.TarError as exc:
        raise BreakGlassError(
            "AWS break-glass bundle is not a valid tar.gz"
        ) from exc
    with archive:
        members = archive.getmembers()
    if not members:
        raise BreakGlassError("AWS break-glass bundle is empty")
    seen: set[str] = set()
    for member in members:
        member_path = Path(member.name)
        normalized = member_path.as_posix().rstrip("/")
        if (
            not normalized
            or normalized in seen
            or member_path.is_absolute()
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
        seen.add(normalized)
    return members


def _safe_install_aws_bundle(data: bytes, destination: Path) -> None:
    members = _validated_aws_members(data)
    if destination.is_symlink():
        raise BreakGlassError(
            "AWS break-glass destination cannot be a symlink"
        )
    destination.mkdir(parents=True, exist_ok=True)
    destination.chmod(0o700)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for member in members:
            target = destination / member.name
            if target.exists() and target.is_symlink():
                raise BreakGlassError(
                    "AWS break-glass target cannot be a symlink: "
                    f"{member.name}"
                )
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                target.chmod(0o700)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise BreakGlassError(
                    "cannot read AWS break-glass bundle member: "
                    f"{member.name}"
                )
            with source, target.open("wb") as handle:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
            target.chmod(0o600)


def _file_rows_from_sources(
    *,
    production_env: Path,
    backup_env: Path,
    restore_env: Path,
    aws_bundle: Path,
) -> dict[str, dict[str, str | int]]:
    sources = {
        "katcha.env": production_env,
        "backup.env": backup_env,
        "restore.env": restore_env,
        "aws_bundle.tgz": aws_bundle,
    }
    files: dict[str, dict[str, str | int]] = {}
    total = 0
    raw_values: dict[str, bytes] = {}
    for name, path in sources.items():
        data = _read_required_file(path, name)
        total += len(data)
        raw_values[name] = data
        files[name] = {
            "base64": base64.b64encode(data).decode("ascii"),
            "sha256": _sha256(data),
            "size_bytes": len(data),
        }
    if total > _MAX_RAW_BUNDLE_BYTES:
        raise BreakGlassError(
            f"break-glass escrow content is too large: {total} bytes"
        )
    _validated_aws_members(raw_values["aws_bundle.tgz"])
    return files


def _decode_file(payload: dict[str, Any], name: str) -> bytes:
    files = payload.get("files")
    if not isinstance(files, dict):
        raise BreakGlassError("break-glass file map is invalid")
    row = files.get(name)
    if not isinstance(row, dict):
        raise BreakGlassError(
            f"break-glass file metadata is invalid: {name}"
        )
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


def _validate_file_map(payload: dict[str, Any]) -> None:
    files = payload.get("files")
    if not isinstance(files, dict) or set(files) != set(_REQUIRED_FILES):
        raise BreakGlassError(
            "break-glass payload does not contain the exact required files"
        )
    total = 0
    decoded: dict[str, bytes] = {}
    for name in _REQUIRED_FILES:
        data = _decode_file(payload, name)
        decoded[name] = data
        total += len(data)
    if total > _MAX_RAW_BUNDLE_BYTES:
        raise BreakGlassError(
            f"break-glass payload content is too large: {total} bytes"
        )
    _validated_aws_members(decoded["aws_bundle.tgz"])


def build_escrow(
    *,
    production_env: Path,
    backup_env: Path,
    restore_env: Path,
    aws_bundle: Path,
    now: datetime | None = None,
) -> bytes:
    current = (now or _utc_now()).astimezone(UTC)
    payload = {
        "format_version": 1,
        "kind": "break-glass-escrow",
        "escrow_id": str(uuid.uuid4()),
        "created_at": current.isoformat(),
        "files": _file_rows_from_sources(
            production_env=production_env,
            backup_env=backup_env,
            restore_env=restore_env,
            aws_bundle=aws_bundle,
        ),
    }
    return (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")


def build_bundle(
    *,
    production_env: Path,
    backup_env: Path,
    restore_env: Path,
    aws_bundle: Path,
    ttl_seconds: int,
    now: datetime | None = None,
) -> bytes:
    current = (now or _utc_now()).astimezone(UTC)
    return _build_handoff_payload(
        files=_file_rows_from_sources(
            production_env=production_env,
            backup_env=backup_env,
            restore_env=restore_env,
            aws_bundle=aws_bundle,
        ),
        ttl_seconds=ttl_seconds,
        now=current,
    )


def _build_handoff_payload(
    *,
    files: dict[str, Any],
    ttl_seconds: int,
    now: datetime | None = None,
) -> bytes:
    if ttl_seconds < 300 or ttl_seconds > 3600:
        raise BreakGlassError(
            "break-glass handoff TTL must be between 300 and 3600 seconds"
        )
    current = (now or _utc_now()).astimezone(UTC)
    payload = {
        "format_version": 1,
        "kind": "break-glass-handoff",
        "handoff_id": str(uuid.uuid4()),
        "created_at": current.isoformat(),
        "expires_at": (
            current + timedelta(seconds=ttl_seconds)
        ).isoformat(),
        "files": files,
    }
    _validate_file_map(payload)
    return (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")


def encrypt_bundle(bundle: bytes) -> tuple[str, bytes]:
    key = Fernet.generate_key()
    encrypted = Fernet(key).encrypt(bundle)
    if len(encrypted) > _MAX_ENCRYPTED_BYTES:
        raise BreakGlassError("encrypted break-glass payload is too large")
    return key.decode("ascii"), encrypted


def _encrypt_with_key(payload: bytes, fernet_key: str) -> bytes:
    try:
        encrypted = Fernet(fernet_key.encode("ascii")).encrypt(payload)
    except (ValueError, UnicodeEncodeError) as exc:
        raise BreakGlassError(
            "break-glass escrow key must be a valid Fernet key"
        ) from exc
    if len(encrypted) > _MAX_ENCRYPTED_BYTES:
        raise BreakGlassError("encrypted break-glass payload is too large")
    return encrypted


def _decrypt_json(encrypted: bytes, fernet_key: str) -> dict[str, Any]:
    if not encrypted or len(encrypted) > _MAX_ENCRYPTED_BYTES:
        raise BreakGlassError(
            "encrypted break-glass payload is empty or unexpectedly large"
        )
    try:
        plaintext = Fernet(fernet_key.encode("ascii")).decrypt(encrypted)
    except (ValueError, InvalidToken, UnicodeEncodeError) as exc:
        raise BreakGlassError(
            "break-glass payload could not be decrypted"
        ) from exc
    try:
        payload = json.loads(plaintext)
    except json.JSONDecodeError as exc:
        raise BreakGlassError(
            "break-glass payload is not valid JSON"
        ) from exc
    if not isinstance(payload, dict) or payload.get("format_version") != 1:
        raise BreakGlassError("unsupported break-glass payload format")
    return payload


def _parse_escrow(
    encrypted: bytes,
    fernet_key: str,
) -> dict[str, Any]:
    payload = _decrypt_json(encrypted, fernet_key)
    if payload.get("kind") != "break-glass-escrow":
        raise BreakGlassError("object is not a break-glass escrow payload")
    _validate_file_map(payload)
    return payload


def _parse_bundle(
    encrypted: bytes,
    fernet_key: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    payload = _decrypt_json(encrypted, fernet_key)
    if payload.get("kind") != "break-glass-handoff":
        raise BreakGlassError("object is not a break-glass handoff payload")
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
    _validate_file_map(payload)
    return payload


def install_bundle(
    encrypted: bytes,
    *,
    fernet_key: str,
    root: Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    payload = _parse_bundle(encrypted, fernet_key, now=now)
    decoded = {
        name: _decode_file(payload, name)
        for name in _REQUIRED_FILES
    }
    if root.is_symlink():
        raise BreakGlassError(
            "break-glass install root cannot be a symlink"
        )
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    for name in ("katcha.env", "backup.env", "restore.env"):
        path = root / name
        if path.exists() and path.is_symlink():
            raise BreakGlassError(
                f"break-glass destination cannot be a symlink: {name}"
            )
        path.write_bytes(decoded[name])
        path.chmod(0o600)
    _safe_install_aws_bundle(
        decoded["aws_bundle.tgz"],
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
            data = response.read(_MAX_ENCRYPTED_BYTES + 1)
    except Exception as exc:
        raise BreakGlassError(
            "could not download break-glass handoff"
        ) from exc
    if not data or len(data) > _MAX_ENCRYPTED_BYTES:
        raise BreakGlassError(
            "downloaded break-glass handoff is empty or unexpectedly large"
        )
    return data


def _r2_get(client, bucket: str, object_key: str) -> bytes:
    response = client.get_object(Bucket=bucket, Key=object_key)
    body = response["Body"]
    try:
        data = body.read(_MAX_ENCRYPTED_BYTES + 1)
    finally:
        body.close()
    if not data or len(data) > _MAX_ENCRYPTED_BYTES:
        raise BreakGlassError(
            "stored break-glass object is empty or unexpectedly large"
        )
    return data


def _validate_object_key(
    object_key: str,
    *,
    prefix: str,
    label: str,
) -> None:
    expected = f"{prefix}/"
    if (
        not object_key.startswith(expected)
        or ".." in Path(object_key).parts
        or object_key.endswith("/")
    ):
        raise BreakGlassError(
            f"invalid {label} object key: {object_key}"
        )


def publish_escrow(
    *,
    production_env: Path,
    backup_env: Path,
    restore_env: Path,
    aws_bundle: Path,
    escrow_key: str,
    config: BreakGlassR2Config,
) -> dict[str, str | int]:
    payload = build_escrow(
        production_env=production_env,
        backup_env=backup_env,
        restore_env=restore_env,
        aws_bundle=aws_bundle,
    )
    encrypted = _encrypt_with_key(payload, escrow_key)
    object_key = (
        f"{config.escrow_prefix}/"
        f"{_utc_now().strftime('%Y%m%dT%H%M%SZ')}-"
        f"{secrets.token_hex(16)}.bin"
    )
    config.client().put_object(
        Bucket=config.bucket,
        Key=object_key,
        Body=encrypted,
        ContentType="application/octet-stream",
        CacheControl="no-store",
    )
    return {
        "format_version": 1,
        "object_key": object_key,
        "ciphertext_sha256": _sha256(encrypted),
        "size_bytes": len(encrypted),
    }


def _upload_handoff(
    *,
    files: dict[str, Any],
    ttl_seconds: int,
    config: BreakGlassR2Config,
) -> dict[str, str | int]:
    bundle = _build_handoff_payload(
        files=files,
        ttl_seconds=ttl_seconds,
    )
    fernet_key, encrypted = encrypt_bundle(bundle)
    object_key = (
        f"{config.handoff_prefix}/"
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


def create_handoff(
    *,
    production_env: Path,
    backup_env: Path,
    restore_env: Path,
    aws_bundle: Path,
    ttl_seconds: int,
    config: BreakGlassR2Config,
) -> dict[str, str | int]:
    payload = json.loads(
        build_bundle(
            production_env=production_env,
            backup_env=backup_env,
            restore_env=restore_env,
            aws_bundle=aws_bundle,
            ttl_seconds=ttl_seconds,
        )
    )
    return _upload_handoff(
        files=payload["files"],
        ttl_seconds=ttl_seconds,
        config=config,
    )


def create_handoff_from_escrow(
    *,
    escrow_object_key: str,
    escrow_key: str,
    ttl_seconds: int,
    config: BreakGlassR2Config,
) -> dict[str, str | int]:
    _validate_object_key(
        escrow_object_key,
        prefix=config.escrow_prefix,
        label="escrow",
    )
    client = config.client()
    encrypted_escrow = _r2_get(
        client,
        config.bucket,
        escrow_object_key,
    )
    escrow = _parse_escrow(encrypted_escrow, escrow_key)
    return _upload_handoff(
        files=escrow["files"],
        ttl_seconds=ttl_seconds,
        config=config,
    )


def delete_handoff(
    object_key: str,
    config: BreakGlassR2Config,
) -> None:
    _validate_object_key(
        object_key,
        prefix=config.handoff_prefix,
        label="handoff",
    )
    config.client().delete_object(
        Bucket=config.bucket,
        Key=object_key,
    )


def delete_escrow(
    object_key: str,
    config: BreakGlassR2Config,
) -> None:
    _validate_object_key(
        object_key,
        prefix=config.escrow_prefix,
        label="escrow",
    )
    config.client().delete_object(
        Bucket=config.bucket,
        Key=object_key,
    )


def _env_escrow_key() -> str:
    value = os.environ.get("KATCHA_BREAK_GLASS_ESCROW_KEY", "").strip()
    if not value:
        raise BreakGlassError(
            "KATCHA_BREAK_GLASS_ESCROW_KEY is required"
        )
    return value


def _cmd_generate_key(_args: argparse.Namespace) -> int:
    print(Fernet.generate_key().decode("ascii"))
    return 0


def _cmd_publish_escrow(args: argparse.Namespace) -> int:
    result = publish_escrow(
        production_env=args.production_env,
        backup_env=args.backup_env,
        restore_env=args.restore_env,
        aws_bundle=args.aws_bundle,
        escrow_key=_env_escrow_key(),
        config=BreakGlassR2Config.from_env(),
    )
    print(json.dumps(result, sort_keys=True))
    return 0


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


def _cmd_create_from_escrow(args: argparse.Namespace) -> int:
    result = create_handoff_from_escrow(
        escrow_object_key=args.escrow_object_key,
        escrow_key=_env_escrow_key(),
        ttl_seconds=args.ttl_seconds,
        config=BreakGlassR2Config.from_env(),
    )
    print(json.dumps(result, sort_keys=True))
    return 0


def _cmd_install(args: argparse.Namespace) -> int:
    url = args.url or os.environ.get(
        "KATCHA_BREAK_GLASS_HANDOFF_URL",
        "",
    ).strip()
    fernet_key = (
        args.fernet_key
        or os.environ.get(
            "KATCHA_BREAK_GLASS_HANDOFF_KEY",
            "",
        ).strip()
    )
    if not url:
        raise BreakGlassError("break-glass handoff URL is required")
    if not fernet_key:
        raise BreakGlassError("break-glass handoff key is required")
    encrypted = download_encrypted(url)
    result = install_bundle(
        encrypted,
        fernet_key=fernet_key,
        root=args.root,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


def _cmd_delete_handoff(args: argparse.Namespace) -> int:
    delete_handoff(
        args.object_key,
        BreakGlassR2Config.from_env(),
    )
    print(json.dumps({"deleted": args.object_key}, sort_keys=True))
    return 0


def _cmd_delete_escrow(args: argparse.Namespace) -> int:
    delete_escrow(
        args.object_key,
        BreakGlassR2Config.from_env(),
    )
    print(json.dumps({"deleted": args.object_key}, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
    )

    generate = subparsers.add_parser("generate-key")
    generate.set_defaults(func=_cmd_generate_key)

    publish = subparsers.add_parser("publish-escrow")
    publish.add_argument("--production-env", type=Path, required=True)
    publish.add_argument("--backup-env", type=Path, required=True)
    publish.add_argument("--restore-env", type=Path, required=True)
    publish.add_argument("--aws-bundle", type=Path, required=True)
    publish.set_defaults(func=_cmd_publish_escrow)

    create = subparsers.add_parser("create-handoff")
    create.add_argument("--production-env", type=Path, required=True)
    create.add_argument("--backup-env", type=Path, required=True)
    create.add_argument("--restore-env", type=Path, required=True)
    create.add_argument("--aws-bundle", type=Path, required=True)
    create.add_argument("--ttl-seconds", type=int, default=1800)
    create.set_defaults(func=_cmd_create)

    create_escrow = subparsers.add_parser(
        "create-handoff-from-escrow"
    )
    create_escrow.add_argument(
        "--escrow-object-key",
        required=True,
    )
    create_escrow.add_argument(
        "--ttl-seconds",
        type=int,
        default=1800,
    )
    create_escrow.set_defaults(func=_cmd_create_from_escrow)

    install = subparsers.add_parser("install-handoff")
    install.add_argument("--url")
    install.add_argument("--fernet-key")
    install.add_argument("--root", type=Path, required=True)
    install.set_defaults(func=_cmd_install)

    delete = subparsers.add_parser("delete-handoff")
    delete.add_argument("--object-key", required=True)
    delete.set_defaults(func=_cmd_delete_handoff)

    delete_escrow_parser = subparsers.add_parser("delete-escrow")
    delete_escrow_parser.add_argument("--object-key", required=True)
    delete_escrow_parser.set_defaults(func=_cmd_delete_escrow)

    args = parser.parse_args()
    try:
        return int(args.func(args))
    except BreakGlassError as exc:
        print(f"BREAK_GLASS_ERROR: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
