from __future__ import annotations

from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from scripts import verify_media_cutover as media


class FakePaginator:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def paginate(self, **_kwargs):
        yield {"Contents": self.rows}


class FakeClient:
    def __init__(
        self,
        *,
        listed: list[dict[str, object]] | None = None,
        heads: dict[str, int] | None = None,
    ) -> None:
        self.listed = listed or []
        self.heads = heads or {}

    def get_paginator(self, name: str):
        assert name == "list_objects_v2"
        return FakePaginator(self.listed)

    def head_object(self, *, Bucket: str, Key: str):
        del Bucket
        if Key not in self.heads:
            raise ClientError(
                {
                    "Error": {"Code": "404", "Message": "missing"},
                    "ResponseMetadata": {"HTTPStatusCode": 404},
                },
                "HeadObject",
            )
        return {"ContentLength": self.heads[Key]}


def _store(bucket: str) -> media.StoreConfig:
    return media.StoreConfig(
        endpoint_url="https://example.invalid",
        access_key="access",
        secret_key="secret",
        bucket=bucket,
        region="auto",
        force_path_style=False,
    )


def test_build_plan_copies_only_missing_or_size_mismatched_objects() -> None:
    source = FakeClient(
        listed=[
            {"Key": "raw/a.mp4", "Size": 10, "ETag": '"a"'},
            {"Key": "raw/b.mp4", "Size": 20, "ETag": '"b"'},
            {"Key": "raw/c.mp4", "Size": 30, "ETag": '"c"'},
        ]
    )
    target = FakeClient(
        heads={
            "raw/a.mp4": 10,
            "raw/b.mp4": 999,
        }
    )

    plan = media.build_plan(
        source,
        target,
        _store("katcha-media"),
        _store("katcha-media-prod"),
    )

    assert plan.total_source_objects == 3
    assert plan.total_source_bytes == 60
    assert plan.already_present_objects == 1
    assert [row.key for row in plan.copy_objects] == [
        "raw/b.mp4",
        "raw/c.mp4",
    ]
    assert plan.copy_bytes == 50


def test_target_env_requires_expected_production_r2_bucket(tmp_path: Path) -> None:
    env = tmp_path / "katcha.env"
    env.write_text(
        "\n".join(
            [
                "KATCHA_S3_ENDPOINT_URL=https://abc.r2.cloudflarestorage.com",
                "KATCHA_S3_ACCESS_KEY=access",
                "KATCHA_S3_SECRET_KEY=secret",
                "KATCHA_S3_BUCKET=wrong-bucket",
                "KATCHA_S3_REGION=auto",
                "KATCHA_S3_FORCE_PATH_STYLE=false",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(media.MediaCutoverError, match="unexpected production media bucket"):
        media.target_from_env(env)


def test_source_defaults_fail_closed_for_non_loopback_endpoint() -> None:
    args = SimpleNamespace(
        source_endpoint="http://192.0.2.50:9000",
        source_access_key="katcha",
        source_secret_key="secret",
        source_bucket="katcha-media",
        source_region="us-east-1",
        allow_remote_source=False,
    )

    with pytest.raises(media.MediaCutoverError, match="must be loopback"):
        media.source_from_args(args)


def test_human_bytes_is_stable() -> None:
    assert media.human_bytes(0) == "0.0 B"
    assert media.human_bytes(1024) == "1.0 KiB"
    assert media.human_bytes(1024 * 1024) == "1.0 MiB"
