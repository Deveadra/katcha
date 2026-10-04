from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

FORMAT_VERSION = 1
MANIFEST_NAME = "manifest.json"
EXPECTED_PAYLOADS = {
    "katcha.env",
    "backup.env",
    "restore.env",
    "aws.tgz",
}
MAX_PAYLOAD_BYTES = 64 * 1024 * 1024
MAX_TOTAL_PAYLOAD_BYTES = 128 * 1024 * 1024
MAX_MANIFEST_BYTES = 256 * 1024
MAX_PLAINTEXT_ARCHIVE_BYTES = MAX_TOTAL_PAYLOAD_BYTES + (2 * 1024 * 1024)


class BreakGlassError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BundleInputs:
    production_env: Path
    backup_env: Path
    restore_env: Path
    aws_bundle: Path

    def payloads(self) -> dict[str, Path]:
        return {
            "katcha.env": self.production_env,
            "backup.env": self.backup_env,
            "restore.env": self.restore_env,
            "aws.tgz": self.aws_bundle,
        }


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _require_private_regular_file(path: Path, *, label: str) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise BreakGlassError(f"{label} does not exist: {path}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise BreakGlassError(f"{label} must not be a symlink: {path}")
    if not stat.S_ISREG(info.st_mode):
        raise BreakGlassError(f"{label} must be a regular file: {path}")
    if info.st_size <= 0:
        raise BreakGlassError(f"{label} is empty: {path}")
    if info.st_size > MAX_PAYLOAD_BYTES:
        raise BreakGlassError(
            f"{label} exceeds the {MAX_PAYLOAD_BYTES}-byte payload limit"
        )


def _read_limited(path: Path, *, label: str) -> bytes:
    _require_private_regular_file(path, label=label)
    data = path.read_bytes()
    if len(data) > MAX_PAYLOAD_BYTES:
        raise BreakGlassError(
            f"{label} exceeds the {MAX_PAYLOAD_BYTES}-byte payload limit"
        )
    return data


def _run_age(args: list[str], *, stdin: BinaryIO | None = None) -> None:
    executable = shutil.which(args[0])
    if executable is None:
        raise BreakGlassError(
            f"{args[0]} is required but was not found on PATH"
        )
    try:
        subprocess.run(
            [executable, *args[1:]],
            stdin=stdin,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=False,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode("utf-8", errors="replace").strip()
        if len(detail) > 500:
            detail = detail[:500] + "..."
        raise BreakGlassError(
            f"{args[0]} failed"
            + (f": {detail}" if detail else "")
        ) from exc


def _validate_recipients_file(path: Path) -> None:
    _require_private_regular_file(path, label="age recipients file")
    recipients = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not recipients:
        raise BreakGlassError("age recipients file contains no recipients")
    for recipient in recipients:
        if not recipient.startswith(("age1", "ssh-")):
            raise BreakGlassError(
                "age recipients file contains an unsupported recipient"
            )


def _validate_identity_file(path: Path) -> None:
    _require_private_regular_file(path, label="age identity file")
    if os.name == "posix":
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            raise BreakGlassError(
                "age identity file must not be readable or writable by group/other"
            )


def _manifest_for(payloads: dict[str, bytes]) -> dict[str, object]:
    created_at = datetime.now(UTC).isoformat()
    rows: list[dict[str, object]] = []
    for name in sorted(payloads):
        data = payloads[name]
        rows.append(
            {
                "name": name,
                "sha256": _sha256_bytes(data),
                "size_bytes": len(data),
            }
        )
    return {
        "format_version": FORMAT_VERSION,
        "created_at": created_at,
        "payload_count": len(rows),
        "payloads": rows,
    }


def _canonical_manifest_bytes(manifest: dict[str, object]) -> bytes:
    return (
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _tar_info(name: str, size: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name=name)
    info.size = size
    info.mode = 0o600
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    return info


def _write_plain_archive(
    target: Path,
    *,
    payloads: dict[str, bytes],
    manifest: dict[str, object],
) -> None:
    manifest_bytes = _canonical_manifest_bytes(manifest)
    with target.open("wb") as handle:
        os.fchmod(handle.fileno(), 0o600)
        with tarfile.open(fileobj=handle, mode="w") as archive:
            archive.addfile(
                _tar_info(MANIFEST_NAME, len(manifest_bytes)),
                io.BytesIO(manifest_bytes),
            )
            for name in sorted(payloads):
                data = payloads[name]
                archive.addfile(
                    _tar_info(name, len(data)),
                    io.BytesIO(data),
                )


def create_bundle(
    inputs: BundleInputs,
    *,
    recipients_file: Path,
    output: Path,
) -> Path:
    _validate_recipients_file(recipients_file)
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    if output.exists():
        raise BreakGlassError(
            f"refusing to overwrite an existing break-glass bundle: {output}"
        )

    payloads: dict[str, bytes] = {}
    total = 0
    for name, path in inputs.payloads().items():
        data = _read_limited(path, label=name)
        total += len(data)
        if total > MAX_TOTAL_PAYLOAD_BYTES:
            raise BreakGlassError(
                "break-glass payloads exceed the total size limit"
            )
        payloads[name] = data

    if set(payloads) != EXPECTED_PAYLOADS:
        raise BreakGlassError("break-glass bundle payload set is invalid")

    manifest = _manifest_for(payloads)
    with tempfile.TemporaryDirectory(prefix="katcha-breakglass-") as temp_dir:
        temp = Path(temp_dir)
        os.chmod(temp, 0o700)
        plain_archive = temp / "bundle.tar"
        encrypted_output = temp / "bundle.age"
        _write_plain_archive(
            plain_archive,
            payloads=payloads,
            manifest=manifest,
        )

        with plain_archive.open("rb") as handle:
            _run_age(
                [
                    "age",
                    "--encrypt",
                    "-R",
                    str(recipients_file.resolve()),
                    "-o",
                    str(encrypted_output),
                ],
                stdin=handle,
            )

        if not encrypted_output.is_file() or encrypted_output.stat().st_size <= 0:
            raise BreakGlassError("age did not produce an encrypted bundle")
        encrypted_output.chmod(0o600)
        os.replace(encrypted_output, output)

    output.chmod(0o600)
    return output


def _safe_tar_members(archive: tarfile.TarFile) -> dict[str, tarfile.TarInfo]:
    members: dict[str, tarfile.TarInfo] = {}
    for member in archive.getmembers():
        name = member.name
        if (
            not name
            or name.startswith("/")
            or name.startswith("../")
            or "/../" in name
            or "\\" in name
            or "/" in name
        ):
            raise BreakGlassError(
                f"break-glass archive contains an unsafe path: {name!r}"
            )
        if not member.isfile():
            raise BreakGlassError(
                f"break-glass archive contains a non-file entry: {name!r}"
            )
        if name in members:
            raise BreakGlassError(
                f"break-glass archive contains a duplicate entry: {name}"
            )
        if member.size < 0 or member.size > MAX_PAYLOAD_BYTES:
            raise BreakGlassError(
                f"break-glass archive entry has an invalid size: {name}"
            )
        members[name] = member

    expected = EXPECTED_PAYLOADS | {MANIFEST_NAME}
    if set(members) != expected:
        missing = sorted(expected - set(members))
        unexpected = sorted(set(members) - expected)
        raise BreakGlassError(
            "break-glass archive entry set is invalid"
            f"; missing={missing}; unexpected={unexpected}"
        )
    if members[MANIFEST_NAME].size > MAX_MANIFEST_BYTES:
        raise BreakGlassError("break-glass manifest is too large")
    total_payload_size = sum(
        members[name].size for name in EXPECTED_PAYLOADS
    )
    if total_payload_size > MAX_TOTAL_PAYLOAD_BYTES:
        raise BreakGlassError(
            "break-glass archive payloads exceed the total size limit"
        )
    return members


def _read_member(
    archive: tarfile.TarFile,
    member: tarfile.TarInfo,
) -> bytes:
    handle = archive.extractfile(member)
    if handle is None:
        raise BreakGlassError(
            f"could not read break-glass archive entry: {member.name}"
        )
    data = handle.read(member.size + 1)
    if len(data) != member.size:
        raise BreakGlassError(
            f"break-glass archive entry size mismatch: {member.name}"
        )
    return data


def _validate_manifest(
    raw: bytes,
    payloads: dict[str, bytes],
) -> dict[str, object]:
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BreakGlassError("break-glass manifest is not valid JSON") from exc
    if not isinstance(manifest, dict):
        raise BreakGlassError("break-glass manifest must be a JSON object")
    if manifest.get("format_version") != FORMAT_VERSION:
        raise BreakGlassError("unsupported break-glass bundle format")
    rows = manifest.get("payloads")
    if not isinstance(rows, list):
        raise BreakGlassError("break-glass manifest payloads are invalid")
    if manifest.get("payload_count") != len(rows):
        raise BreakGlassError("break-glass manifest payload count is inconsistent")

    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise BreakGlassError("break-glass manifest contains an invalid row")
        name = str(row.get("name") or "")
        if name in seen:
            raise BreakGlassError(
                f"break-glass manifest contains a duplicate payload: {name}"
            )
        seen.add(name)
        if name not in EXPECTED_PAYLOADS:
            raise BreakGlassError(
                f"break-glass manifest contains an unexpected payload: {name}"
            )
        data = payloads.get(name)
        if data is None:
            raise BreakGlassError(
                f"break-glass manifest references a missing payload: {name}"
            )
        if int(row.get("size_bytes") or -1) != len(data):
            raise BreakGlassError(
                f"break-glass payload size does not match manifest: {name}"
            )
        if str(row.get("sha256") or "") != _sha256_bytes(data):
            raise BreakGlassError(
                f"break-glass payload checksum does not match manifest: {name}"
            )
    if seen != EXPECTED_PAYLOADS:
        raise BreakGlassError("break-glass manifest payload set is incomplete")
    return manifest


def inspect_plain_archive(path: Path) -> dict[str, object]:
    _require_private_regular_file(path, label="plaintext break-glass archive")
    with tarfile.open(path, mode="r:*") as archive:
        members = _safe_tar_members(archive)
        payloads = {
            name: _read_member(archive, members[name])
            for name in EXPECTED_PAYLOADS
        }
        manifest_raw = _read_member(archive, members[MANIFEST_NAME])
        return _validate_manifest(manifest_raw, payloads)


def extract_bundle(
    bundle: Path,
    *,
    identity_file: Path,
    destination: Path,
) -> Path:
    _require_private_regular_file(bundle, label="encrypted break-glass bundle")
    _validate_identity_file(identity_file)
    destination = destination.resolve()
    if destination.exists():
        if not destination.is_dir():
            raise BreakGlassError(
                f"break-glass destination is not a directory: {destination}"
            )
        if any(destination.iterdir()):
            raise BreakGlassError(
                f"break-glass destination must be empty: {destination}"
            )
    else:
        destination.mkdir(parents=True, mode=0o700)
    destination.chmod(0o700)

    with tempfile.TemporaryDirectory(prefix="katcha-breakglass-") as temp_dir:
        temp = Path(temp_dir)
        os.chmod(temp, 0o700)
        plain_archive = temp / "bundle.tar"
        with plain_archive.open("wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            executable = shutil.which("age")
            if executable is None:
                raise BreakGlassError("age is required but was not found on PATH")
            process = subprocess.Popen(
                [
                    executable,
                    "--decrypt",
                    "-i",
                    str(identity_file.resolve()),
                    str(bundle.resolve()),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            assert process.stdout is not None
            written = 0
            try:
                while True:
                    chunk = process.stdout.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > MAX_PLAINTEXT_ARCHIVE_BYTES:
                        process.kill()
                        process.wait()
                        raise BreakGlassError(
                            "decrypted break-glass archive exceeds the size limit"
                        )
                    handle.write(chunk)
                stderr = process.stderr.read() if process.stderr is not None else b""
                return_code = process.wait()
            finally:
                process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()

            if return_code != 0:
                detail = stderr.decode("utf-8", errors="replace").strip()
                if len(detail) > 500:
                    detail = detail[:500] + "..."
                raise BreakGlassError(
                    "age decryption failed"
                    + (f": {detail}" if detail else "")
                )

        with tarfile.open(plain_archive, mode="r:*") as archive:
            members = _safe_tar_members(archive)
            payloads = {
                name: _read_member(archive, members[name])
                for name in EXPECTED_PAYLOADS
            }
            manifest_raw = _read_member(archive, members[MANIFEST_NAME])
            manifest = _validate_manifest(manifest_raw, payloads)

        for name, data in payloads.items():
            target = destination / name
            with target.open("xb") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(data)

        manifest_target = destination / MANIFEST_NAME
        with manifest_target.open("xb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(_canonical_manifest_bytes(manifest))

    return destination


def _cmd_create(args: argparse.Namespace) -> int:
    path = create_bundle(
        BundleInputs(
            production_env=args.production_env,
            backup_env=args.backup_env,
            restore_env=args.restore_env,
            aws_bundle=args.aws_bundle,
        ),
        recipients_file=args.recipients_file,
        output=args.output,
    )
    print(path)
    return 0


def _cmd_extract(args: argparse.Namespace) -> int:
    path = extract_bundle(
        args.bundle,
        identity_file=args.identity_file,
        destination=args.destination,
    )
    print(path)
    return 0


def _cmd_inspect(args: argparse.Namespace) -> int:
    manifest = inspect_plain_archive(args.archive)
    print(
        json.dumps(
            {
                "format_version": manifest["format_version"],
                "created_at": manifest["created_at"],
                "payload_count": manifest["payload_count"],
                "payload_names": sorted(
                    row["name"]
                    for row in manifest["payloads"]
                    if isinstance(row, dict)
                ),
            },
            sort_keys=True,
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create")
    create.add_argument("--production-env", type=Path, required=True)
    create.add_argument("--backup-env", type=Path, required=True)
    create.add_argument("--restore-env", type=Path, required=True)
    create.add_argument("--aws-bundle", type=Path, required=True)
    create.add_argument("--recipients-file", type=Path, required=True)
    create.add_argument("--output", type=Path, required=True)
    create.set_defaults(func=_cmd_create)

    extract = subparsers.add_parser("extract")
    extract.add_argument("--bundle", type=Path, required=True)
    extract.add_argument("--identity-file", type=Path, required=True)
    extract.add_argument("--destination", type=Path, required=True)
    extract.set_defaults(func=_cmd_extract)

    inspect = subparsers.add_parser("inspect-plain")
    inspect.add_argument("--archive", type=Path, required=True)
    inspect.set_defaults(func=_cmd_inspect)

    args = parser.parse_args()
    try:
        return int(args.func(args))
    except BreakGlassError as exc:
        print(f"BREAKGLASS_ERROR: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
