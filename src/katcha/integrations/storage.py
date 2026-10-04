from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from katcha.config import Settings, get_settings
from katcha.runtime_fence import assert_mutation_authority


class ObjectStore:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.client = self._client(self.settings.s3_endpoint_url)

    def _client(self, endpoint_url: str | None):
        config = Config(
            s3={"addressing_style": "path" if self.settings.s3_force_path_style else "auto"}
        )
        client_kwargs: dict[str, Any] = {
            "region_name": self.settings.s3_region,
            "config": config,
        }
        if endpoint_url:
            client_kwargs["endpoint_url"] = endpoint_url
        if self.settings.s3_access_key and self.settings.s3_secret_key:
            client_kwargs["aws_access_key_id"] = self.settings.s3_access_key
            client_kwargs["aws_secret_access_key"] = self.settings.s3_secret_key
        return boto3.client("s3", **client_kwargs)

    def ensure_bucket(self) -> None:
        try:
            self.client.head_bucket(Bucket=self.settings.s3_bucket)
            return
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code") or "")
            status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") or 0)
            if code not in {"404", "NoSuchBucket", "NotFound"} and status != 404:
                raise
        assert_mutation_authority(
            "storage.create_bucket",
            settings=self.settings,
        )
        kwargs: dict[str, object] = {"Bucket": self.settings.s3_bucket}
        if self.settings.s3_region not in {"auto", "us-east-1"}:
            kwargs["CreateBucketConfiguration"] = {
                "LocationConstraint": self.settings.s3_region
            }
        self.client.create_bucket(**kwargs)

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.settings.s3_bucket, Key=key)
            return True
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise

    def stat(self, key: str) -> dict[str, Any]:
        response = self.client.head_object(Bucket=self.settings.s3_bucket, Key=key)
        return {
            "size_bytes": int(response.get("ContentLength") or 0),
            "content_type": response.get("ContentType"),
            "etag": str(response.get("ETag") or "").strip('"'),
        }

    def put_file(self, path: Path, key: str, content_type: str | None = None) -> None:
        assert_mutation_authority(
            "storage.put_file",
            settings=self.settings,
        )
        extra = {"ContentType": content_type} if content_type else None
        if extra:
            self.client.upload_file(str(path), self.settings.s3_bucket, key, ExtraArgs=extra)
        else:
            self.client.upload_file(str(path), self.settings.s3_bucket, key)

    def put_bytes(self, data: bytes, key: str, content_type: str | None = None) -> None:
        assert_mutation_authority(
            "storage.put_bytes",
            settings=self.settings,
        )
        kwargs: dict[str, object] = {
            "Bucket": self.settings.s3_bucket,
            "Key": key,
            "Body": data,
        }
        if content_type:
            kwargs["ContentType"] = content_type
        self.client.put_object(**kwargs)

    def download_file(self, key: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        self.client.download_file(self.settings.s3_bucket, key, str(destination))

    def get_bytes(self, key: str, *, max_bytes: int | None = None) -> bytes:
        response = self.client.get_object(Bucket=self.settings.s3_bucket, Key=key)
        body = response["Body"]
        try:
            data = body.read() if max_bytes is None else body.read(max_bytes + 1)
            if max_bytes is not None and len(data) > max_bytes:
                raise ValueError("Stored object exceeds the allowed size")
            return data
        finally:
            body.close()

    def presigned_get_url(
        self,
        key: str,
        *,
        expires_seconds: int,
        filename: str | None = None,
        endpoint_url: str | None = None,
    ) -> str:
        params: dict[str, object] = {
            "Bucket": self.settings.s3_bucket,
            "Key": key,
        }
        if filename:
            params["ResponseContentDisposition"] = f'inline; filename="{filename}"'
            params["ResponseContentType"] = "video/mp4"
        client = (
            self.client
            if endpoint_url in {None, self.settings.s3_endpoint_url}
            else self._client(endpoint_url)
        )
        return str(
            client.generate_presigned_url(
                "get_object",
                Params=params,
                ExpiresIn=expires_seconds,
            )
        )

    def delete(self, key: str) -> None:
        assert_mutation_authority(
            "storage.delete",
            settings=self.settings,
        )
        self.client.delete_object(Bucket=self.settings.s3_bucket, Key=key)

    def delete_many(self, keys: list[str]) -> None:
        unique = [key for key in dict.fromkeys(keys) if key]
        if not unique:
            return
        assert_mutation_authority(
            "storage.delete_many",
            settings=self.settings,
        )
        for index in range(0, len(unique), 1000):
            batch = unique[index : index + 1000]
            self.client.delete_objects(
                Bucket=self.settings.s3_bucket,
                Delete={"Objects": [{"Key": key} for key in batch], "Quiet": True},
            )

    def move(self, source_key: str, destination_key: str) -> None:
        if source_key == destination_key:
            return
        assert_mutation_authority(
            "storage.move",
            settings=self.settings,
        )
        self.client.copy_object(
            Bucket=self.settings.s3_bucket,
            Key=destination_key,
            CopySource={"Bucket": self.settings.s3_bucket, "Key": source_key},
            MetadataDirective="COPY",
        )
        self.client.delete_object(Bucket=self.settings.s3_bucket, Key=source_key)

    def iter_range(
        self,
        key: str,
        start: int,
        end: int,
        chunk_size: int = 1024 * 1024,
    ) -> Iterator[bytes]:
        response = self.client.get_object(
            Bucket=self.settings.s3_bucket,
            Key=key,
            Range=f"bytes={start}-{end}",
        )
        body = response["Body"]
        try:
            while chunk := body.read(chunk_size):
                yield chunk
        finally:
            body.close()

    def iter_bytes(
        self,
        key: str,
        chunk_size: int = 1024 * 1024,
    ) -> Iterator[bytes]:
        response = self.client.get_object(Bucket=self.settings.s3_bucket, Key=key)
        body = response["Body"]
        try:
            while chunk := body.read(chunk_size):
                yield chunk
        finally:
            body.close()

    @staticmethod
    def raw_key(sha256: str, extension: str | None) -> str:
        suffix = f".{extension.lstrip('.')}" if extension else ""
        return f"raw/{sha256[:2]}/{sha256}{suffix}"

    @staticmethod
    def archive_key(sha256: str, extension: str | None) -> str:
        suffix = f".{extension.lstrip('.')}" if extension else ""
        return f"archive/raw/{sha256[:2]}/{sha256}{suffix}"

    @staticmethod
    def analysis_key(sha256: str, name: str) -> str:
        safe_name = name.lstrip("/")
        return f"analysis/{sha256[:2]}/{sha256}/{safe_name}"

    @staticmethod
    def production_key(production_id: str, name: str) -> str:
        safe_name = name.lstrip("/")
        return f"production/{production_id}/{safe_name}"

    @staticmethod
    def compilation_key(compilation_id: str, name: str) -> str:
        safe_name = name.lstrip("/")
        return f"compilation/{compilation_id}/{safe_name}"

    @staticmethod
    def short_episode_key(episode_id: str, name: str) -> str:
        safe_name = name.lstrip("/")
        return f"short-episode/{episode_id}/{safe_name}"
