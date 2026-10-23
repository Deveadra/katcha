#!/usr/bin/env python3
"""Install private worker credentials or check metadata and inert Docker mounts."""

import argparse
import getpass
import os
import re
import stat
import subprocess
import sys
import uuid
from pathlib import Path

UID = 10001
CF_FILES = ("cf-access-id", "cf-access-secret")


def safe_path(path):
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Credential directory must be an absolute normalized path")
    for part in (*reversed(path.parents), path):
        if part.is_symlink():
            raise ValueError("Symlink paths are not permitted")
        if part.exists() and (not part.is_dir() or part.stat().st_mode & 0o022):
            raise ValueError(
                "Directory ancestors must be directories without group/world write access"
            )


def check_files(directory, names):
    safe_path(directory)
    info = directory.stat()
    if (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (UID, UID, 0o700):
        raise ValueError("Credential directory must be owned by 10001:10001 with mode 0700")
    for name in names:
        info = (directory / name).lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError(f"{name}: must be a regular file with one link")
        if (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (UID, UID, 0o600):
            raise ValueError(f"{name}: requires owner 10001:10001 and mode 0600")
        if info.st_size == 0:
            raise ValueError(f"{name}: file is empty")


def install(directory, names):
    safe_path(directory)
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise ValueError("Installation requires an interactive terminal for hidden input")
    if any((directory / name).exists() or (directory / name).is_symlink() for name in names):
        raise ValueError(
            "Refusing to replace existing credentials; use check-only for installed files"
        )
    values = {}
    for name in names:
        value = getpass.getpass(f"{name} (hidden): ")
        if not value or any(c.isspace() for c in value):
            raise ValueError("Credentials must be nonempty and contain no whitespace")
        if value != getpass.getpass(f"Confirm {name} (hidden): "):
            raise ValueError("Confirmation did not match; no files installed")
        values[name] = value
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chown(directory, UID, UID)
    directory.chmod(0o700)
    created = []
    try:
        for name, value in values.items():
            path = directory / name
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            created.append(path)
            with os.fdopen(fd, "w") as stream:
                os.fchown(stream.fileno(), UID, UID)
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
    except BaseException:
        for path in created:
            path.unlink()
        raise


def verify_mounts(directory, names, image):
    if not re.fullmatch(r"(sha256:[0-9a-f]{64}|[^\s]+@sha256:[0-9a-f]{64})", image):
        raise ValueError("Use an existing immutable image ID or repository digest")
    # No pulls, networking, worker process, or secret reads. Override image entrypoint.
    container_name = "katcha-credential-check-" + uuid.uuid4().hex
    command = [
        "docker",
        "run",
        "--rm",
        "--pull=never",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--user=10001:10001",
        "--memory=128m",
        "--cpus=0.1",
        "--pids-limit=32",
        "--name=" + container_name,
    ]
    for name in names:
        command += [
            "--mount",
            f"type=bind,src={directory / name},dst=/run/acquisition/{name},readonly",
        ]
    probe = """import os, pathlib, stat, sys
for name in sys.argv[1:]:
    p = pathlib.Path('/run/acquisition') / name
    s = p.stat()
    assert stat.S_ISREG(s.st_mode) and s.st_uid == 10001 and s.st_gid == 10001
    assert stat.S_IMODE(s.st_mode) == 0o600 and s.st_size > 0
    fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW)
    os.close(fd)
    assert os.statvfs(p).f_flag & os.ST_RDONLY
print('PASS: UID 10001 access; read-only mounts; no contents read')
"""
    command += ["--entrypoint=python", image, "-c", probe, *names]
    try:
        subprocess.run(command, check=True, timeout=60)
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--directory", type=Path, default=Path("/etc/katcha-acquisition/credentials")
    )
    parser.add_argument("--kind", choices=("cloudflare", "worker", "all"), default="cloudflare")
    parser.add_argument(
        "--check-only", action="store_true", help="Do not install or change host files"
    )
    parser.add_argument(
        "--image", required=True, help="Already-local Python image ID or repository digest"
    )
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Run as the authorized root deployment operator (sudo)")
    if "," in str(args.directory):
        parser.error("Credential path may not contain commas")
    names = CF_FILES if args.kind == "cloudflare" else ("worker-token",)
    if args.kind == "all":
        names = (*CF_FILES, "worker-token")
    try:
        # Validate image before accepting any secrets.
        if not re.fullmatch(r"(sha256:[0-9a-f]{64}|[^\s]+@sha256:[0-9a-f]{64})", args.image):
            raise ValueError("Use an immutable image ID or repository digest")
        subprocess.run(
            ["docker", "image", "inspect", args.image],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=15,
        )
        if not args.check_only:
            install(args.directory, names)
        check_files(args.directory, names)
        verify_mounts(args.directory, names, args.image)
        print(
            "Credential checks passed. Worker was not activated. "
            "Release and authorization gates still apply."
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        # Never echo exception payloads, inputs, subprocess command lines, or secrets.
        print(
            "FAILED: check path safety, owner/mode, selected files, "
            "and local Docker image availability. "
            "No worker was activated. Installed files may remain if container verification failed.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
