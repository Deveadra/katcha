#!/usr/bin/env python3
"""Build the small AWS credential-only OCI Vault bundle on a trusted host.

The public aws_signing_helper binary MUST NOT be included: the replacement VM
fetches version 1.8.5 from AWS and verifies its pinned SHA-256 separately.
This command never prints credentials or writes to stdout.
"""
import argparse
import io
import os
import stat
import sys
import tarfile
from pathlib import Path

MAX_BYTES = 24_000
REQUIRED_FILES = (
    "config",
    "runtime/client.pem",
    "runtime/client-key.pem",
)


def pack(aws_dir: Path, output: Path) -> int:
    if not aws_dir.is_dir() or aws_dir.is_symlink():
        raise ValueError("AWS credential directory is absent or a symlink")
    if not (aws_dir / "runtime").is_dir() or (aws_dir / "runtime").is_symlink():
        raise ValueError("AWS runtime directory is absent or a symlink")
    files = []
    for relative in REQUIRED_FILES:
        path = aws_dir / relative
        mode = path.lstat().st_mode
        if not stat.S_ISREG(mode) or path.is_symlink():
            raise ValueError(f"unsafe AWS bundle member: {relative}")
        if path.stat().st_size == 0:
            raise ValueError(f"empty AWS bundle member: {relative}")
        files.append((path, relative))

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path, relative in files:
            archive.add(path, arcname=relative, recursive=False)
    data = buffer.getvalue()
    if len(data) > MAX_BYTES:
        raise ValueError(
            f"credential-only archive is {len(data)} bytes, exceeding "
            f"the {MAX_BYTES}-byte safety ceiling"
        )
    if not data:
        raise ValueError("empty credential-only archive")
    if not output.parent.is_dir():
        raise ValueError("protected output directory does not exist")

    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(output, flags, 0o600)
    with os.fdopen(descriptor, "wb") as destination:
        destination.write(data)
    print(f"AWS_VAULT_CREDENTIAL_BUNDLE_READY bytes={len(data)}")
    return len(data)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aws-dir", type=Path, default=Path("/etc/katcha/aws"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        pack(args.aws_dir, args.output)
    except (OSError, ValueError, tarfile.TarError) as error:
        print(f"STOP: AWS Vault bundle preparation failed: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
