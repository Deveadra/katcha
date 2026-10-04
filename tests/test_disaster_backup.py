from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from katcha.ops import disaster_backup


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.operations: list[tuple[str, str]] = []

    def head_object(self, *, Bucket, Key):
        value = self.objects.get((Bucket, Key))
        if value is None:
            raise ClientError(
                {
                    "Error": {"Code": "404"},
                    "ResponseMetadata": {"HTTPStatusCode": 404},
                },
                "HeadObject",
            )
        return {"ContentLength": len(value)}

    def upload_file(self, filename, bucket, key, ExtraArgs=None):
        del ExtraArgs
        self.objects[(bucket, key)] = Path(filename).read_bytes()
        self.operations.append(("upload_file", key))

    def put_object(self, *, Bucket, Key, Body, ContentType=None):
        del ContentType
        data = Body.encode() if isinstance(Body, str) else bytes(Body)
        self.objects[(Bucket, Key)] = data
        self.operations.append(("put_object", Key))
        return {}

    def list_objects_v2(self, *, Bucket, Prefix, MaxKeys, ContinuationToken=None):
        del MaxKeys, ContinuationToken
        keys = sorted(
            key
            for bucket, key in self.objects
            if bucket == Bucket and key.startswith(Prefix)
        )
        return {
            "Contents": [{"Key": key} for key in keys],
            "IsTruncated": False,
        }

    def get_object(self, *, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def download_file(self, bucket, key, filename):
        Path(filename).write_bytes(self.objects[(bucket, key)])


def _config(client: FakeS3, monkeypatch) -> disaster_backup.R2BackupConfig:
    config = disaster_backup.R2BackupConfig(
        endpoint_url="https://account.r2.cloudflarestorage.com",
        access_key="writer",
        secret_key="secret",
        session_token=None,
        bucket="katcha-backup-prod",
        prefix="postgres",
        region="auto",
        force_path_style=False,
    )
    monkeypatch.setattr(
        disaster_backup.R2BackupConfig,
        "client",
        lambda self: client,
    )
    return config


def _backup_directory(tmp_path: Path) -> Path:
    directory = tmp_path / "20261003T010203Z-test"
    directory.mkdir()
    (directory / "db-a.dump").write_bytes(b"katcha-dump")
    (directory / "db-b.dump").write_bytes(b"temporal-dump")
    mapping = directory / "databases.tsv"
    mapping.write_text(
        "db-a.dump\tkatcha\ndb-b.dump\ttemporal\n",
        encoding="utf-8",
    )
    disaster_backup.create_manifest(
        directory=directory,
        mapping_path=mapping,
        backup_id=directory.name,
        release_sha="a" * 40,
        deployment_id="oci-a1-primary",
        deployment_epoch=7,
    )
    return directory


def test_manifest_and_roundtrip_are_checksum_verified(tmp_path, monkeypatch) -> None:
    source = _backup_directory(tmp_path)
    s3 = FakeS3()
    config = _config(s3, monkeypatch)
    monkeypatch.setattr(
        disaster_backup,
        "assert_mutation_authority",
        lambda _operation: None,
    )

    complete_key = disaster_backup.upload_backup(source, config)

    assert complete_key.endswith("/_COMPLETE.json")
    assert s3.operations[-1] == ("put_object", complete_key)

    restore_root = tmp_path / "restore"
    restored = disaster_backup.download_latest(restore_root, config)

    assert (restored / "db-a.dump").read_bytes() == b"katcha-dump"
    assert (restored / "db-b.dump").read_bytes() == b"temporal-dump"
    manifest = disaster_backup.load_manifest(restored / "manifest.json")
    assert {row["database"] for row in manifest["databases"]} == {
        "katcha",
        "temporal",
    }


def test_upload_refuses_to_overwrite_completed_generation(tmp_path, monkeypatch) -> None:
    source = _backup_directory(tmp_path)
    s3 = FakeS3()
    config = _config(s3, monkeypatch)
    monkeypatch.setattr(
        disaster_backup,
        "assert_mutation_authority",
        lambda _operation: None,
    )

    disaster_backup.upload_backup(source, config)
    with pytest.raises(
        disaster_backup.DisasterBackupError,
        match="refusing to overwrite",
    ):
        disaster_backup.upload_backup(source, config)


def test_download_rejects_tampered_dump(tmp_path, monkeypatch) -> None:
    source = _backup_directory(tmp_path)
    s3 = FakeS3()
    config = _config(s3, monkeypatch)
    monkeypatch.setattr(
        disaster_backup,
        "assert_mutation_authority",
        lambda _operation: None,
    )

    disaster_backup.upload_backup(source, config)
    dump_key = "postgres/20261003T010203Z-test/db-a.dump"
    s3.objects[(config.bucket, dump_key)] = b"tampered"

    with pytest.raises(
        disaster_backup.DisasterBackupError,
        match="size mismatch|checksum mismatch",
    ):
        disaster_backup.download_latest(tmp_path / "restore", config)


def test_manifest_rejects_missing_or_empty_database_dumps(tmp_path) -> None:
    directory = tmp_path / "backup"
    directory.mkdir()
    mapping = directory / "databases.tsv"
    mapping.write_text("missing.dump\tkatcha\n", encoding="utf-8")

    with pytest.raises(disaster_backup.DisasterBackupError, match="missing"):
        disaster_backup.create_manifest(
            directory=directory,
            mapping_path=mapping,
            backup_id="backup-1",
            release_sha="b" * 40,
            deployment_id="deployment-1",
            deployment_epoch=1,
        )



def test_manifest_age_rejects_stale_recovery_point(tmp_path) -> None:
    source = _backup_directory(tmp_path)
    manifest = disaster_backup.load_manifest(source / "manifest.json")
    manifest["created_at"] = (
        datetime(2026, 10, 4, 0, 0, tzinfo=UTC).isoformat()
    )

    with pytest.raises(disaster_backup.DisasterBackupError, match="too old"):
        disaster_backup.validate_manifest_age(
            manifest,
            max_age_seconds=3600,
            now=datetime(2026, 10, 4, 2, 0, tzinfo=UTC),
        )


def test_manifest_age_accepts_recent_recovery_point(tmp_path) -> None:
    source = _backup_directory(tmp_path)
    manifest = disaster_backup.load_manifest(source / "manifest.json")
    now = datetime(2026, 10, 4, 2, 0, tzinfo=UTC)
    manifest["created_at"] = (now - timedelta(minutes=45)).isoformat()

    disaster_backup.validate_manifest_age(
        manifest,
        max_age_seconds=3600,
        now=now,
    )
