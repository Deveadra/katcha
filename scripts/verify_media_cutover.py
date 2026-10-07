#!/usr/bin/env python3
"""Inspect or copy the frozen local MinIO media bucket into production R2.

Default mode is read-only. Same-size objects are SHA-256 compared. --apply copies
only objects that fail the comparison and never deletes target objects.
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError


class MediaCutoverError(RuntimeError):
    pass


@dataclass(frozen=True)
class StoreConfig:
    endpoint_url: str
    access_key: str
    secret_key: str
    bucket: str
    region: str
    force_path_style: bool

    def client(self):
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


@dataclass(frozen=True)
class ObjectRow:
    key: str
    size: int
    etag: str


@dataclass(frozen=True)
class CopyPlan:
    total_source_objects: int
    total_source_bytes: int
    already_present_objects: int
    copy_objects: tuple[ObjectRow, ...]

    @property
    def copy_bytes(self) -> int:
        return sum(item.size for item in self.copy_objects)


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        raise MediaCutoverError(f"production environment file not found: {path}")
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


def required(values: dict[str, str], key: str) -> str:
    value = values.get(key, "").strip()
    if not value:
        raise MediaCutoverError(f"production environment is missing {key}")
    return value


def target_from_env(path: Path) -> StoreConfig:
    values = read_env(path)
    endpoint = required(values, "KATCHA_S3_ENDPOINT_URL")
    parsed = urlsplit(endpoint)
    if parsed.scheme != "https" or not (parsed.hostname or "").endswith(
        ".r2.cloudflarestorage.com"
    ):
        raise MediaCutoverError("target must be a Cloudflare R2 HTTPS endpoint")
    bucket = required(values, "KATCHA_S3_BUCKET")
    if bucket != "katcha-media-prod":
        raise MediaCutoverError(
            f"refusing unexpected production media bucket: {bucket}"
        )
    if values.get("KATCHA_S3_REGION", "").strip() != "auto":
        raise MediaCutoverError("production R2 region must be auto")
    if values.get("KATCHA_S3_FORCE_PATH_STYLE", "").strip().casefold() != "false":
        raise MediaCutoverError("production R2 path-style mode must be false")
    return StoreConfig(
        endpoint_url=endpoint,
        access_key=required(values, "KATCHA_S3_ACCESS_KEY"),
        secret_key=required(values, "KATCHA_S3_SECRET_KEY"),
        bucket=bucket,
        region="auto",
        force_path_style=False,
    )


def source_from_args(args: argparse.Namespace) -> StoreConfig:
    endpoint = args.source_endpoint.rstrip("/")
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"}:
        raise MediaCutoverError("source endpoint must use HTTP or HTTPS")
    if (
        parsed.hostname not in {"127.0.0.1", "localhost"}
        and not args.allow_remote_source
    ):
        raise MediaCutoverError(
            "source endpoint must be loopback unless --allow-remote-source is explicit"
        )
    return StoreConfig(
        endpoint_url=endpoint,
        access_key=args.source_access_key,
        secret_key=args.source_secret_key,
        bucket=args.source_bucket,
        region=args.source_region,
        force_path_style=True,
    )


def assert_source_frozen(repo_root: Path) -> None:
    def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            argv,
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            detail = result.stderr.strip() or f"exit {result.returncode}"
            raise MediaCutoverError(
                f"local Docker inspection failed for {' '.join(argv)}: {detail}"
            )
        return result

    minio = run(["docker", "compose", "ps", "-q", "minio"]).stdout.strip()
    minio_ids = [line.strip() for line in minio.splitlines() if line.strip()]
    if len(minio_ids) != 1:
        raise MediaCutoverError(
            "expected exactly one local Katcha MinIO container for media handoff"
        )

    project = run(
        [
            "docker",
            "inspect",
            "--format",
            '{{ index .Config.Labels "com.docker.compose.project" }}',
            minio_ids[0],
        ]
    ).stdout.strip()
    if not project:
        raise MediaCutoverError("local MinIO container has no Compose project label")

    services = run(
        [
            "docker",
            "ps",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--format",
            '{{.Label "com.docker.compose.service"}}',
        ]
    ).stdout.splitlines()
    unsafe = sorted(
        service.strip()
        for service in services
        if service.strip() and service.strip() not in {"postgres", "minio"}
    )
    if unsafe:
        raise MediaCutoverError(
            "local media source is not frozen; running mutating services: "
            + ", ".join(unsafe)
        )


def assert_object_access(client, bucket: str) -> None:
    # Object-scoped R2 credentials can list/read/write objects without bucket
    # administration permission, so do not probe with HeadBucket here.
    client.list_objects_v2(Bucket=bucket, MaxKeys=1)


def list_objects(client, bucket: str) -> list[ObjectRow]:
    rows: list[ObjectRow] = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket):
        for item in page.get("Contents", []):
            key = str(item.get("Key") or "")
            if not key:
                continue
            rows.append(
                ObjectRow(
                    key=key,
                    size=int(item.get("Size") or 0),
                    etag=str(item.get("ETag") or "").strip('"'),
                )
            )
    rows.sort(key=lambda item: item.key)
    return rows


def object_sha256(client, bucket: str, key: str) -> str:
    response = client.get_object(Bucket=bucket, Key=key)
    body = response["Body"]
    digest = hashlib.sha256()
    try:
        while True:
            chunk = body.read(8 * 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    finally:
        body.close()
    return digest.hexdigest()


def build_plan(
    source_client,
    target_client,
    source: StoreConfig,
    target: StoreConfig,
) -> CopyPlan:
    source_rows = list_objects(source_client, source.bucket)
    copy_rows: list[ObjectRow] = []
    present = 0
    for row in source_rows:
        try:
            target_head = target_client.head_object(Bucket=target.bucket, Key=row.key)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code") or "")
            status = int(
                exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") or 0
            )
            if code not in {"404", "NoSuchKey", "NotFound"} and status != 404:
                raise
            copy_rows.append(row)
            continue
        target_size = int(target_head.get("ContentLength") or 0)
        if target_size != row.size:
            copy_rows.append(row)
            continue

        source_digest = object_sha256(source_client, source.bucket, row.key)
        target_digest = object_sha256(target_client, target.bucket, row.key)
        if source_digest == target_digest:
            present += 1
        else:
            copy_rows.append(row)
    return CopyPlan(
        total_source_objects=len(source_rows),
        total_source_bytes=sum(row.size for row in source_rows),
        already_present_objects=present,
        copy_objects=tuple(copy_rows),
    )


def copy_object(
    source_client,
    target_client,
    source: StoreConfig,
    target: StoreConfig,
    row: ObjectRow,
) -> None:
    response = source_client.get_object(Bucket=source.bucket, Key=row.key)
    body = response["Body"]
    extra: dict[str, str] = {}
    for source_name, target_name in (
        ("ContentType", "ContentType"),
        ("ContentDisposition", "ContentDisposition"),
        ("CacheControl", "CacheControl"),
        ("ContentEncoding", "ContentEncoding"),
        ("ContentLanguage", "ContentLanguage"),
    ):
        value = response.get(source_name)
        if value:
            extra[target_name] = str(value)
    metadata = response.get("Metadata")
    if isinstance(metadata, dict) and metadata:
        extra["Metadata"] = metadata
    try:
        if extra:
            target_client.upload_fileobj(
                body,
                target.bucket,
                row.key,
                ExtraArgs=extra,
            )
        else:
            target_client.upload_fileobj(
                body,
                target.bucket,
                row.key,
            )
    finally:
        body.close()
    head = target_client.head_object(Bucket=target.bucket, Key=row.key)
    if int(head.get("ContentLength") or -1) != row.size:
        raise MediaCutoverError(
            f"target size verification failed after copy: {row.key}"
        )
    source_digest = object_sha256(source_client, source.bucket, row.key)
    target_digest = object_sha256(target_client, target.bucket, row.key)
    if source_digest != target_digest:
        raise MediaCutoverError(
            f"target SHA-256 verification failed after copy: {row.key}"
        )


def human_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--production-env",
        default=str(
            Path.home()
            / ".config"
            / "katcha"
            / "production"
            / "handoff"
            / "katcha.env"
        ),
    )
    parser.add_argument("--source-endpoint", default="http://127.0.0.1:9000")
    parser.add_argument("--source-access-key", default="katcha")
    parser.add_argument("--source-secret-key", default="katcha-local-secret")
    parser.add_argument("--source-bucket", default="katcha-media")
    parser.add_argument("--source-region", default="us-east-1")
    parser.add_argument("--allow-remote-source", action="store_true")
    parser.add_argument("--repo-root", default=".")
    args = parser.parse_args()

    try:
        if args.apply:
            assert_source_frozen(Path(args.repo_root).resolve())
        source = source_from_args(args)
        target = target_from_env(Path(args.production_env))
        source_client = source.client()
        target_client = target.client()

        assert_object_access(source_client, source.bucket)
        assert_object_access(target_client, target.bucket)

        plan = build_plan(source_client, target_client, source, target)
        print(
            "MEDIA_CUTOVER_PLAN "
            f"source_objects={plan.total_source_objects} "
            f"source_bytes={human_bytes(plan.total_source_bytes)} "
            f"already_present={plan.already_present_objects} "
            f"copy_objects={len(plan.copy_objects)} "
            f"copy_bytes={human_bytes(plan.copy_bytes)}"
        )

        if not args.apply:
            print("INSPECT_ONLY no media objects changed")
            return 0

        copied = 0
        copied_bytes = 0
        for row in plan.copy_objects:
            print(f"COPY {row.key} {human_bytes(row.size)}")
            copy_object(source_client, target_client, source, target, row)
            copied += 1
            copied_bytes += row.size

        final_plan = build_plan(source_client, target_client, source, target)
        if final_plan.copy_objects:
            raise MediaCutoverError(
                f"media verification still needs {len(final_plan.copy_objects)} objects"
            )
        print(
            "MEDIA_CUTOVER_COMPLETE "
            f"copied_objects={copied} "
            f"copied_bytes={human_bytes(copied_bytes)} "
            f"verified_source_objects={final_plan.total_source_objects}"
        )
    except (MediaCutoverError, BotoCoreError, ClientError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
