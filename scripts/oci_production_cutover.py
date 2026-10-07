#!/usr/bin/env python3
"""Fail-closed operator tooling for the first OCI production cutover."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class CutoverError(RuntimeError):
    pass


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
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


def required_value(name: str, local_env: dict[str, str], fallback_file: Path | None = None) -> str:
    value = os.environ.get(name, "").strip() or local_env.get(name, "").strip()
    if not value and fallback_file and fallback_file.is_file():
        value = fallback_file.read_text(encoding="utf-8").strip()
    if not value:
        raise CutoverError(f"missing required value: {name}")
    return value


@dataclass(frozen=True)
class AdminPath:
    base: Path
    key: Path
    port: int = 22022
    host: str = "127.0.0.1"
    user: str = "ubuntu"

    @property
    def known_hosts(self) -> Path:
        return self.base / "target_known_hosts"

    def ssh_argv(self, *remote: str) -> list[str]:
        return [
            "ssh",
            "-F",
            "/dev/null",
            "-i",
            str(self.key),
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            f"UserKnownHostsFile={self.known_hosts}",
            "-o",
            "ConnectTimeout=10",
            "-p",
            str(self.port),
            f"{self.user}@{self.host}",
            *remote,
        ]

    def scp_argv(self, *paths: str) -> list[str]:
        # Deliberately separate from ssh_argv: scp requires upper-case -P.
        return [
            "scp",
            "-F",
            "/dev/null",
            "-i",
            str(self.key),
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            f"UserKnownHostsFile={self.known_hosts}",
            "-P",
            str(self.port),
            *paths,
        ]

    def assert_ready(self) -> None:
        if not self.key.is_file():
            raise CutoverError(f"missing target SSH key: {self.key}")
        try:
            with socket.create_connection((self.host, self.port), timeout=3):
                pass
        except OSError as exc:
            raise CutoverError(
                f"Bastion forward is not accepting TCP on {self.host}:{self.port}"
            ) from exc
        result = subprocess.run(
            self.ssh_argv("true"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if result.returncode:
            detail = result.stderr.strip() or f"exit {result.returncode}"
            raise CutoverError(f"target SSH is not usable through Bastion: {detail}")

    def run_script(
        self,
        script: str,
        *,
        sudo: bool,
        args: tuple[str, ...] = (),
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        remote = ["sudo", "bash", "-s", "--"] if sudo else ["bash", "-s", "--"]
        remote.extend(args)
        return subprocess.run(
            self.ssh_argv(*remote),
            input=script,
            text=True,
            check=check,
        )


def package_snapshot(snapshot_dir: Path, archive: Path) -> None:
    mapping = snapshot_dir / "databases.tsv"
    sums = snapshot_dir / "SHA256SUMS"
    ready = snapshot_dir / "READY"
    for path in (mapping, sums, ready):
        if not path.is_file():
            raise CutoverError(f"snapshot is incomplete: missing {path.name}")

    databases: list[str] = []
    for raw in mapping.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        parts = raw.split("\t")
        if len(parts) != 2:
            raise CutoverError(f"invalid databases.tsv row: {raw!r}")
        filename = parts[0]
        candidate = Path(filename)
        if candidate.is_absolute() or candidate.name != filename or not filename.endswith(".dump"):
            raise CutoverError(f"unsafe dump filename: {filename!r}")
        databases.append(filename)
    if not databases:
        raise CutoverError("snapshot contains no database dumps")

    expected: dict[str, str] = {}
    for raw in sums.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        digest, filename = raw.split(maxsplit=1)
        filename = filename.lstrip("*")
        if filename.startswith("./"):
            filename = filename[2:]
        expected[filename] = digest.lower()

    for filename in databases:
        path = snapshot_dir / filename
        if not path.is_file():
            raise CutoverError(f"snapshot dump is missing: {filename}")
        if filename not in expected:
            raise CutoverError(f"snapshot checksum is missing: {filename}")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected[filename]:
            raise CutoverError(f"snapshot checksum mismatch: {filename}")

    archive.parent.mkdir(parents=True, exist_ok=True)
    temporary = archive.with_name(archive.name + ".tmp")
    temporary.unlink(missing_ok=True)
    names = ["databases.tsv", "SHA256SUMS", "READY", *databases]
    created_at = snapshot_dir / "created-at"
    if created_at.is_file():
        names.insert(2, "created-at")
    with tarfile.open(temporary, "w:gz") as handle:
        for name in names:
            handle.add(snapshot_dir / name, arcname=name, recursive=False)
    os.chmod(temporary, 0o600)
    temporary.replace(archive)


def request_json(
    method: str,
    url: str,
    token: str,
    payload: dict[str, Any] | None = None,
    timeout: float = 15,
) -> dict[str, Any]:
    data = None
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":")).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise CutoverError(f"{method} {url} failed with HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise CutoverError(f"{method} {url} failed: {exc.reason}") from exc
    if not raw:
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise CutoverError(f"{method} {url} returned unexpected JSON")
    return value


def wait_public_health(url: str, attempts: int = 60) -> None:
    last = "not attempted"
    for _ in range(attempts):
        try:
            request = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
            with urllib.request.urlopen(request, timeout=8) as response:
                if 200 <= response.status < 300:
                    response.read()
                    return
                last = f"HTTP {response.status}"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = str(exc)
        time.sleep(2)
    raise CutoverError(f"public health did not become ready: {last}")


REMOTE_INSPECT = r"""
set -euo pipefail
mountpoint -q /srv/katcha
test -s /srv/katcha/postgres/PG_VERSION
compose() {
    docker compose --project-name katcha-production \
        --env-file /etc/katcha/katcha.env \
        -f /opt/katcha/deploy/docker-compose.production.yml "$@"
}
running="$(compose ps --status running --services)"
unsafe="$(printf '%s\n' "$running" | grep -Ev '^(postgres)?test -n "$cid"
for db in katcha temporal temporal_visibility; do
    tables="$(docker exec "$cid" psql -U katcha -d "$db" -At -c "SELECT count(*) FROM pg_catalog.pg_tables WHERE schemaname NOT IN ('pg_catalog','information_schema');")"
    [[ "$tables" =~ ^[0-9]+$ && "$tables" -gt 0 ]]
done
echo HOST_CUTOVER_STATE_READY
"""


REMOTE_PREPARE = r"""
set -euo pipefail
epoch="$1"
deployment="$2"
release="$3"
[[ "$epoch" =~ ^[1-9][0-9]*$ ]]
[[ "$deployment" =~ ^[A-Za-z0-9._:-]+$ ]]
[[ "$release" =~ ^[0-9a-f]{40}$ ]]

test -z "$(git -C /opt/katcha status --porcelain)"
git -C /opt/katcha fetch --force origin "$release"
git -C /opt/katcha checkout --detach "$release"

python3 - /etc/katcha/katcha.env "$epoch" "$deployment" "$release" <<'PY'
from pathlib import Path
import os
import sys
path = Path(sys.argv[1])
values = {
    "KATCHA_DEPLOYMENT_EPOCH": sys.argv[2],
    "KATCHA_DEPLOYMENT_ID": sys.argv[3],
    "KATCHA_RELEASE_SHA": sys.argv[4],
}
out = []
seen = set()
for line in path.read_text(encoding="utf-8").splitlines():
    key = line.split("=", 1)[0].strip() if "=" in line else ""
    if key in values:
        out.append(f"{key}={values[key]}")
        seen.add(key)
    else:
        out.append(line)
for key, value in values.items():
    if key not in seen:
        out.append(f"{key}={value}")
tmp = path.with_name(path.name + ".tmp")
tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
os.chmod(tmp, 0o600)
os.chown(tmp, 0, 0)
tmp.replace(path)
PY
KATCHA_REPO_ROOT=/opt/katcha /bin/bash /opt/katcha/deploy/scripts/install-production-units.sh
PYTHONPATH=/opt/katcha/src python3 /opt/katcha/scripts/validate_production_runtime.py --env-file /etc/katcha/katcha.env
PYTHONPATH=/opt/katcha/src python3 -m katcha.ops.disaster_recovery_validate --production-env /etc/katcha/katcha.env --backup-env /etc/katcha/backup.env --restore-env /etc/katcha/restore.env
docker compose --project-name katcha-production --env-file /etc/katcha/katcha.env -f /opt/katcha/deploy/docker-compose.production.yml config --quiet
echo HOST_CONFIGURATION_VALID
"""


REMOTE_START = r"""
set -euo pipefail
systemctl start katcha.service
systemctl start katcha-backup.timer
systemctl start katcha-restore-test.timer
for attempt in $(seq 1 180); do
    if curl -fsS --max-time 5 http://127.0.0.1:8000/v1/health/ready >/dev/null 2>&1; then
        echo HOST_RUNTIME_READY
        exit 0
    fi
    if [[ "$attempt" -eq 180 ]]; then
        systemctl status katcha.service --no-pager >&2 || true
        journalctl -u katcha.service -n 120 --no-pager >&2 || true
        exit 30
    fi
    sleep 2
done
"""


REMOTE_DURABLE = r"""
set -euo pipefail
mountpoint -q /srv/katcha
cid="$(docker compose --project-name katcha-production --env-file /etc/katcha/katcha.env -f /opt/katcha/deploy/docker-compose.production.yml ps -q postgres)"
test -n "$cid"
for db in katcha temporal temporal_visibility; do
    tables="$(docker exec "$cid" psql -U katcha -d "$db" -At -c "SELECT count(*) FROM pg_catalog.pg_tables WHERE schemaname NOT IN ('pg_catalog','information_schema');")"
    [[ "$tables" =~ ^[0-9]+$ && "$tables" -gt 0 ]]
done
revision="$(docker exec "$cid" psql -U katcha -d katcha -At -c "SELECT version_num FROM alembic_version LIMIT 1;")"
test -n "$revision"
echo HOST_DURABLE_STATE_READY
"""


REMOTE_STOP = r"""
set +e
systemctl stop katcha-backup.timer katcha-restore-test.timer
systemctl stop katcha.service
"""


def fence_assert(recovery_url: str, token: str, deployment: str, epoch: int) -> dict[str, Any]:
    return request_json(
        "POST",
        f"{recovery_url}/v1/fence/assert",
        token,
        {"deployment_id": deployment, "deployment_epoch": epoch},
    )


def activate(args: argparse.Namespace) -> None:
    local_env = read_env(Path(args.local_env))
    recovery_url = (
        args.recovery_url
        or os.environ.get("KATCHA_RECOVERY_COORDINATOR_URL", "")
        or local_env.get("KATCHA_RECOVERY_COORDINATOR_URL", "")
        or "https://recovery.katcha.stream"
    ).rstrip("/")
    admin_token = required_value("KATCHA_RECOVERY_ADMIN_TOKEN", local_env)
    candidate_token = required_value("KATCHA_RECOVERY_CANDIDATE_TOKEN", local_env)
    fence_token = required_value(
        "KATCHA_FENCE_TOKEN",
        local_env,
        Path.home() / ".config/katcha/production/recovery-fence-token",
    )

    admin = AdminPath(
        base=Path(args.bastion_base),
        key=Path(args.ssh_key),
        port=args.port,
        user=args.user,
    )
    admin.assert_ready()
    admin.run_script(REMOTE_INSPECT, sudo=True)

    status = request_json("GET", f"{recovery_url}/v1/authority/status", admin_token)
    active = status.get("active")
    active_epoch = int(active.get("epoch", 0)) if isinstance(active, dict) else 0

    if isinstance(active, dict) and active.get("deployment_id") == args.deployment_id:
        epoch = int(active.get("epoch", 0))
        fence = fence_assert(recovery_url, fence_token, args.deployment_id, epoch)
        if fence.get("authorized") is not True:
            raise CutoverError("deployment is active but the fence denies it")
        wait_public_health(args.health_url, attempts=3)
        print(f"ALREADY_ACTIVE deployment={args.deployment_id} epoch={epoch}")
        return

    if active_epoch != args.expected_active_epoch:
        raise CutoverError(
            f"active epoch changed: expected {args.expected_active_epoch}, found {active_epoch}"
        )
    pending = status.get("pending")
    if pending and (
        not isinstance(pending, dict)
        or pending.get("deployment_id") != args.deployment_id
        or pending.get("health_url") != args.health_url
    ):
        raise CutoverError("another deployment is already pending")

    if len(args.release_sha) != 40 or any(
        char not in "0123456789abcdef" for char in args.release_sha
    ):
        raise CutoverError("release SHA must be an exact lower-case 40-character Git SHA")

    print(
        "INITIAL_PRIMARY_PREFLIGHT_OK "
        f"active_epoch={active_epoch} release={args.release_sha}"
    )
    if not args.apply:
        print("INSPECT_ONLY no authority or host state changed")
        return

    prepared = request_json(
        "POST",
        f"{recovery_url}/v1/authority/prepare",
        admin_token,
        {
            "deployment_id": args.deployment_id,
            "health_url": args.health_url,
            "expected_active_epoch": args.expected_active_epoch,
        },
    )
    prepared_pending = prepared.get("pending")
    if not isinstance(prepared_pending, dict):
        raise CutoverError("authority prepare returned no pending deployment")
    epoch = int(prepared_pending.get("epoch", 0))
    if epoch <= 0:
        raise CutoverError("authority prepare returned invalid epoch")

    commit_attempted = False
    try:
        admin.run_script(
            REMOTE_PREPARE,
            sudo=True,
            args=(str(epoch), args.deployment_id, args.release_sha),
        )
        admin.run_script(REMOTE_START, sudo=True)
        admin.run_script(REMOTE_DURABLE, sudo=True)
        wait_public_health(args.health_url)

        before = fence_assert(recovery_url, fence_token, args.deployment_id, epoch)
        if before.get("authorized") is not False:
            raise CutoverError("candidate was unexpectedly authorized before commit")
        if int(before.get("active_epoch", 0)) != args.expected_active_epoch:
            raise CutoverError("fence active epoch changed before commit")

        request_json(
            "POST",
            f"{recovery_url}/v1/authority/candidate-ready",
            candidate_token,
            {
                "deployment_id": args.deployment_id,
                "deployment_epoch": epoch,
                "readiness": {
                    "runtime_ready": True,
                    "durable_state_ready": True,
                    "fence_probe_ready": True,
                    "public_route_ready": True,
                },
            },
        )

        commit_attempted = True
        try:
            committed = request_json(
                "POST",
                f"{recovery_url}/v1/authority/commit",
                admin_token,
                {
                    "deployment_id": args.deployment_id,
                    "deployment_epoch": epoch,
                    "expected_active_epoch": args.expected_active_epoch,
                },
            )
        except CutoverError as exc:
            reconciled = request_json(
                "GET",
                f"{recovery_url}/v1/authority/status",
                admin_token,
            )
            reconciled_active = reconciled.get("active")
            if not (
                isinstance(reconciled_active, dict)
                and reconciled_active.get("deployment_id") == args.deployment_id
                and int(reconciled_active.get("epoch", 0)) == epoch
            ):
                raise CutoverError(
                    "authority commit outcome is uncertain; candidate was left running "
                    "for manual reconciliation"
                ) from exc
            committed = {"active_epoch": epoch, "leader_id": args.deployment_id}

        if int(committed.get("active_epoch", 0)) != epoch:
            raise CutoverError("commit returned unexpected active epoch")
        if committed.get("leader_id") != args.deployment_id:
            raise CutoverError("commit returned unexpected leader")

        after = fence_assert(recovery_url, fence_token, args.deployment_id, epoch)
        if after.get("authorized") is not True:
            raise CutoverError("active leader is still denied by the fence")
        wait_public_health(args.health_url, attempts=10)
        print(f"INITIAL_PRIMARY_ACTIVE deployment={args.deployment_id} epoch={epoch}")
    except Exception:
        if not commit_attempted:
            admin.run_script(REMOTE_STOP, sudo=True, check=False)
            try:
                request_json(
                    "POST",
                    f"{recovery_url}/v1/authority/abort",
                    admin_token,
                    {
                        "deployment_id": args.deployment_id,
                        "deployment_epoch": epoch,
                    },
                )
            except Exception as abort_error:
                print(f"WARNING: pending authority abort failed: {abort_error}", file=sys.stderr)
        raise


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)

    package = sub.add_parser("package-snapshot")
    package.add_argument("--snapshot-dir", required=True)
    package.add_argument("--archive", required=True)

    activate_parser = sub.add_parser("activate-primary")
    activate_parser.add_argument("--apply", action="store_true")
    activate_parser.add_argument("--local-env", default=".env")
    activate_parser.add_argument("--recovery-url")
    activate_parser.add_argument("--health-url", default="https://app.katcha.stream/v1/health/ready")
    activate_parser.add_argument("--deployment-id", default="oci-a1-primary-001")
    activate_parser.add_argument("--expected-active-epoch", type=int, default=0)
    activate_parser.add_argument("--release-sha", required=True)
    activate_parser.add_argument(
        "--bastion-base",
        default=str(Path.home() / ".config/katcha/production/bastion"),
    )
    activate_parser.add_argument("--ssh-key", default=str(Path.home() / ".ssh/katcha-oci"))
    activate_parser.add_argument("--port", type=int, default=22022)
    activate_parser.add_argument("--user", default="ubuntu")
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "package-snapshot":
            package_snapshot(Path(args.snapshot_dir), Path(args.archive))
            print(f"SNAPSHOT_ARCHIVE_READY {args.archive}")
        elif args.command == "activate-primary":
            activate(args)
        else:
            raise AssertionError(args.command)
    except (CutoverError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
 || true)"
if [[ -n "$unsafe" ]]; then
    echo "unexpected hosted services already running:" >&2
    printf '%s\n' "$unsafe" >&2
    exit 20
fi
cid="$(compose ps -q postgres)"
test -n "$cid"
for db in katcha temporal temporal_visibility; do
    tables="$(docker exec "$cid" psql -U katcha -d "$db" -At -c "SELECT count(*) FROM pg_catalog.pg_tables WHERE schemaname NOT IN ('pg_catalog','information_schema');")"
    [[ "$tables" =~ ^[0-9]+$ && "$tables" -gt 0 ]]
done
echo HOST_CUTOVER_STATE_READY
"""


REMOTE_PREPARE = r"""
set -euo pipefail
epoch="$1"
deployment="$2"
release="$3"
[[ "$epoch" =~ ^[1-9][0-9]*$ ]]
[[ "$deployment" =~ ^[A-Za-z0-9._:-]+$ ]]
[[ "$release" =~ ^[0-9a-f]{40}$ ]]

test -z "$(git -C /opt/katcha status --porcelain)"
git -C /opt/katcha fetch --force origin "$release"
git -C /opt/katcha checkout --detach "$release"

python3 - /etc/katcha/katcha.env "$epoch" "$deployment" "$release" <<'PY'
from pathlib import Path
import os
import sys
path = Path(sys.argv[1])
values = {
    "KATCHA_DEPLOYMENT_EPOCH": sys.argv[2],
    "KATCHA_DEPLOYMENT_ID": sys.argv[3],
    "KATCHA_RELEASE_SHA": sys.argv[4],
}
out = []
seen = set()
for line in path.read_text(encoding="utf-8").splitlines():
    key = line.split("=", 1)[0].strip() if "=" in line else ""
    if key in values:
        out.append(f"{key}={values[key]}")
        seen.add(key)
    else:
        out.append(line)
for key, value in values.items():
    if key not in seen:
        out.append(f"{key}={value}")
tmp = path.with_name(path.name + ".tmp")
tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
os.chmod(tmp, 0o600)
os.chown(tmp, 0, 0)
tmp.replace(path)
PY
KATCHA_REPO_ROOT=/opt/katcha /bin/bash /opt/katcha/deploy/scripts/install-production-units.sh
PYTHONPATH=/opt/katcha/src python3 /opt/katcha/scripts/validate_production_runtime.py --env-file /etc/katcha/katcha.env
PYTHONPATH=/opt/katcha/src python3 -m katcha.ops.disaster_recovery_validate --production-env /etc/katcha/katcha.env --backup-env /etc/katcha/backup.env --restore-env /etc/katcha/restore.env
docker compose --project-name katcha-production --env-file /etc/katcha/katcha.env -f /opt/katcha/deploy/docker-compose.production.yml config --quiet
echo HOST_CONFIGURATION_VALID
"""


REMOTE_START = r"""
set -euo pipefail
systemctl start katcha.service
systemctl start katcha-backup.timer
systemctl start katcha-restore-test.timer
for attempt in $(seq 1 180); do
    if curl -fsS --max-time 5 http://127.0.0.1:8000/v1/health/ready >/dev/null 2>&1; then
        echo HOST_RUNTIME_READY
        exit 0
    fi
    if [[ "$attempt" -eq 180 ]]; then
        systemctl status katcha.service --no-pager >&2 || true
        journalctl -u katcha.service -n 120 --no-pager >&2 || true
        exit 30
    fi
    sleep 2
done
"""


REMOTE_DURABLE = r"""
set -euo pipefail
mountpoint -q /srv/katcha
cid="$(docker compose --project-name katcha-production --env-file /etc/katcha/katcha.env -f /opt/katcha/deploy/docker-compose.production.yml ps -q postgres)"
test -n "$cid"
for db in katcha temporal temporal_visibility; do
    tables="$(docker exec "$cid" psql -U katcha -d "$db" -At -c "SELECT count(*) FROM pg_catalog.pg_tables WHERE schemaname NOT IN ('pg_catalog','information_schema');")"
    [[ "$tables" =~ ^[0-9]+$ && "$tables" -gt 0 ]]
done
revision="$(docker exec "$cid" psql -U katcha -d katcha -At -c "SELECT version_num FROM alembic_version LIMIT 1;")"
test -n "$revision"
echo HOST_DURABLE_STATE_READY
"""


REMOTE_STOP = r"""
set +e
systemctl stop katcha-backup.timer katcha-restore-test.timer
systemctl stop katcha.service
"""


def fence_assert(recovery_url: str, token: str, deployment: str, epoch: int) -> dict[str, Any]:
    return request_json(
        "POST",
        f"{recovery_url}/v1/fence/assert",
        token,
        {"deployment_id": deployment, "deployment_epoch": epoch},
    )


def activate(args: argparse.Namespace) -> None:
    local_env = read_env(Path(args.local_env))
    recovery_url = (
        args.recovery_url
        or os.environ.get("KATCHA_RECOVERY_COORDINATOR_URL", "")
        or local_env.get("KATCHA_RECOVERY_COORDINATOR_URL", "")
        or "https://recovery.katcha.stream"
    ).rstrip("/")
    admin_token = required_value("KATCHA_RECOVERY_ADMIN_TOKEN", local_env)
    candidate_token = required_value("KATCHA_RECOVERY_CANDIDATE_TOKEN", local_env)
    fence_token = required_value(
        "KATCHA_FENCE_TOKEN",
        local_env,
        Path.home() / ".config/katcha/production/recovery-fence-token",
    )

    admin = AdminPath(
        base=Path(args.bastion_base),
        key=Path(args.ssh_key),
        port=args.port,
        user=args.user,
    )
    admin.assert_ready()
    admin.run_script(REMOTE_INSPECT, sudo=True)

    status = request_json("GET", f"{recovery_url}/v1/authority/status", admin_token)
    active = status.get("active")
    active_epoch = int(active.get("epoch", 0)) if isinstance(active, dict) else 0

    if isinstance(active, dict) and active.get("deployment_id") == args.deployment_id:
        epoch = int(active.get("epoch", 0))
        fence = fence_assert(recovery_url, fence_token, args.deployment_id, epoch)
        if fence.get("authorized") is not True:
            raise CutoverError("deployment is active but the fence denies it")
        wait_public_health(args.health_url, attempts=3)
        print(f"ALREADY_ACTIVE deployment={args.deployment_id} epoch={epoch}")
        return

    if active_epoch != args.expected_active_epoch:
        raise CutoverError(
            f"active epoch changed: expected {args.expected_active_epoch}, found {active_epoch}"
        )
    pending = status.get("pending")
    if pending and (
        not isinstance(pending, dict)
        or pending.get("deployment_id") != args.deployment_id
        or pending.get("health_url") != args.health_url
    ):
        raise CutoverError("another deployment is already pending")

    if len(args.release_sha) != 40 or any(
        char not in "0123456789abcdef" for char in args.release_sha
    ):
        raise CutoverError("release SHA must be an exact lower-case 40-character Git SHA")

    print(
        "INITIAL_PRIMARY_PREFLIGHT_OK "
        f"active_epoch={active_epoch} release={args.release_sha}"
    )
    if not args.apply:
        print("INSPECT_ONLY no authority or host state changed")
        return

    prepared = request_json(
        "POST",
        f"{recovery_url}/v1/authority/prepare",
        admin_token,
        {
            "deployment_id": args.deployment_id,
            "health_url": args.health_url,
            "expected_active_epoch": args.expected_active_epoch,
        },
    )
    prepared_pending = prepared.get("pending")
    if not isinstance(prepared_pending, dict):
        raise CutoverError("authority prepare returned no pending deployment")
    epoch = int(prepared_pending.get("epoch", 0))
    if epoch <= 0:
        raise CutoverError("authority prepare returned invalid epoch")

    commit_attempted = False
    try:
        admin.run_script(
            REMOTE_PREPARE,
            sudo=True,
            args=(str(epoch), args.deployment_id, args.release_sha),
        )
        admin.run_script(REMOTE_START, sudo=True)
        admin.run_script(REMOTE_DURABLE, sudo=True)
        wait_public_health(args.health_url)

        before = fence_assert(recovery_url, fence_token, args.deployment_id, epoch)
        if before.get("authorized") is not False:
            raise CutoverError("candidate was unexpectedly authorized before commit")
        if int(before.get("active_epoch", 0)) != args.expected_active_epoch:
            raise CutoverError("fence active epoch changed before commit")

        request_json(
            "POST",
            f"{recovery_url}/v1/authority/candidate-ready",
            candidate_token,
            {
                "deployment_id": args.deployment_id,
                "deployment_epoch": epoch,
                "readiness": {
                    "runtime_ready": True,
                    "durable_state_ready": True,
                    "fence_probe_ready": True,
                    "public_route_ready": True,
                },
            },
        )

        commit_attempted = True
        try:
            committed = request_json(
                "POST",
                f"{recovery_url}/v1/authority/commit",
                admin_token,
                {
                    "deployment_id": args.deployment_id,
                    "deployment_epoch": epoch,
                    "expected_active_epoch": args.expected_active_epoch,
                },
            )
        except CutoverError as exc:
            reconciled = request_json(
                "GET",
                f"{recovery_url}/v1/authority/status",
                admin_token,
            )
            reconciled_active = reconciled.get("active")
            if not (
                isinstance(reconciled_active, dict)
                and reconciled_active.get("deployment_id") == args.deployment_id
                and int(reconciled_active.get("epoch", 0)) == epoch
            ):
                raise CutoverError(
                    "authority commit outcome is uncertain; candidate was left running "
                    "for manual reconciliation"
                ) from exc
            committed = {"active_epoch": epoch, "leader_id": args.deployment_id}

        if int(committed.get("active_epoch", 0)) != epoch:
            raise CutoverError("commit returned unexpected active epoch")
        if committed.get("leader_id") != args.deployment_id:
            raise CutoverError("commit returned unexpected leader")

        after = fence_assert(recovery_url, fence_token, args.deployment_id, epoch)
        if after.get("authorized") is not True:
            raise CutoverError("active leader is still denied by the fence")
        wait_public_health(args.health_url, attempts=10)
        print(f"INITIAL_PRIMARY_ACTIVE deployment={args.deployment_id} epoch={epoch}")
    except Exception:
        if not commit_attempted:
            admin.run_script(REMOTE_STOP, sudo=True, check=False)
            try:
                request_json(
                    "POST",
                    f"{recovery_url}/v1/authority/abort",
                    admin_token,
                    {
                        "deployment_id": args.deployment_id,
                        "deployment_epoch": epoch,
                    },
                )
            except Exception as abort_error:
                print(f"WARNING: pending authority abort failed: {abort_error}", file=sys.stderr)
        raise


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)

    package = sub.add_parser("package-snapshot")
    package.add_argument("--snapshot-dir", required=True)
    package.add_argument("--archive", required=True)

    activate_parser = sub.add_parser("activate-primary")
    activate_parser.add_argument("--apply", action="store_true")
    activate_parser.add_argument("--local-env", default=".env")
    activate_parser.add_argument("--recovery-url")
    activate_parser.add_argument("--health-url", default="https://app.katcha.stream/v1/health/ready")
    activate_parser.add_argument("--deployment-id", default="oci-a1-primary-001")
    activate_parser.add_argument("--expected-active-epoch", type=int, default=0)
    activate_parser.add_argument("--release-sha", required=True)
    activate_parser.add_argument(
        "--bastion-base",
        default=str(Path.home() / ".config/katcha/production/bastion"),
    )
    activate_parser.add_argument("--ssh-key", default=str(Path.home() / ".ssh/katcha-oci"))
    activate_parser.add_argument("--port", type=int, default=22022)
    activate_parser.add_argument("--user", default="ubuntu")
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "package-snapshot":
            package_snapshot(Path(args.snapshot_dir), Path(args.archive))
            print(f"SNAPSHOT_ARCHIVE_READY {args.archive}")
        elif args.command == "activate-primary":
            activate(args)
        else:
            raise AssertionError(args.command)
    except (CutoverError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
