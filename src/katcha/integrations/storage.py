from __future__ import annotations

from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from katcha.config import Settings, get_settings


class ObjectStore:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        config = Config(
            s3={"addressing_style": "path" if self.settings.s3_force_path_style else "auto"}
        )
        self.client = boto3.client(
            "s3",
            endpoint_url=self.settings.s3_endpoint_url,
            aws_access_key_id=self.settings.s3_access_key,
            aws_secret_access_key=self.settings.s3_secret_key,
            region_name=self.settings.s3_region,
            config=config,
        )

    def ensure_bucket(self) -> None:
        try:
            self.client.head_bucket(Bucket=self.settings.s3_bucket)
            return
        except ClientError:
            pass
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

    def put_file(self, path: Path, key: str, content_type: str | None = None) -> None:
        extra = {"ContentType": content_type} if content_type else None
        if extra:
            self.client.upload_file(str(path), self.settings.s3_bucket, key, ExtraArgs=extra)
        else:
            self.client.upload_file(str(path), self.settings.s3_bucket, key)

    def download_file(self, key: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        self.client.download_file(self.settings.s3_bucket, key, str(destination))

    def get_bytes(self, key: str) -> bytes:
        response = self.client.get_object(Bucket=self.settings.s3_bucket, Key=key)
        return response["Body"].read()

    @staticmethod
    def raw_key(sha256: str, extension: str | None) -> str:
        suffix = f".{extension.lstrip('.')}" if extension else ""
        return f"raw/{sha256[:2]}/{sha256}{suffix}"

    @staticmethod
    def analysis_key(sha256: str, name: str) -> str:
        safe_name = name.lstrip("/")
        return f"analysis/{sha256[:2]}/{sha256}/{safe_name}"
