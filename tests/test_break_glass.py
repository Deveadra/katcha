from __future__ import annotations

import io
import json
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from katcha.ops import break_glass


def _aws_bundle(path: Path, *, unsafe: bool = False) -> None:
    with tarfile.open(path, "w:gz") as archive:
        payload = b"[default]\nregion=us-east-1\n"
        info = tarfile.TarInfo(
            "../escape" if unsafe else "config"
        )
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))


def _source_files(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    production = tmp_path / "production.env"
    backup = tmp_path / "backup.env"
    restore = tmp_path / "restore.env"
    aws = tmp_path / "aws.tgz"
    production.write_text(
        "KATCHA_ENV=production\n"
        "KATCHA_CREDENTIAL_ENCRYPTION_KEY=fixture-key\n",
        encoding="utf-8",
    )
    backup.write_text(
        "KATCHA_BACKUP_R2_BUCKET=backup\n",
        encoding="utf-8",
    )
    restore.write_text(
        "KATCHA_RESTORE_R2_BUCKET=backup\n",
        encoding="utf-8",
    )
    _aws_bundle(aws)
    return production, backup, restore, aws


def test_break_glass_bundle_round_trip_installs_exact_files(tmp_path) -> None:
    production, backup, restore, aws = _source_files(tmp_path)
    now = datetime(2026, 10, 4, 2, 30, tzinfo=UTC)

    bundle = break_glass.build_bundle(
        production_env=production,
        backup_env=backup,
        restore_env=restore,
        aws_bundle=aws,
        ttl_seconds=1800,
        now=now,
    )
    key, encrypted = break_glass.encrypt_bundle(bundle)

    root = tmp_path / "installed"
    receipt = break_glass.install_bundle(
        encrypted,
        fernet_key=key,
        root=root,
        now=now + timedelta(minutes=5),
    )

    assert receipt["handoff_id"]
    assert (root / "katcha.env").read_bytes() == production.read_bytes()
    assert (root / "backup.env").read_bytes() == backup.read_bytes()
    assert (root / "restore.env").read_bytes() == restore.read_bytes()
    assert (root / "aws" / "config").read_text(encoding="utf-8") == (
        "[default]\nregion=us-east-1\n"
    )
    assert (root / "katcha.env").stat().st_mode & 0o777 == 0o600
    assert (root / "aws" / "config").stat().st_mode & 0o777 == 0o600


def test_break_glass_bundle_rejects_expired_handoff(tmp_path) -> None:
    production, backup, restore, aws = _source_files(tmp_path)
    now = datetime(2026, 10, 4, 2, 30, tzinfo=UTC)
    bundle = break_glass.build_bundle(
        production_env=production,
        backup_env=backup,
        restore_env=restore,
        aws_bundle=aws,
        ttl_seconds=300,
        now=now,
    )
    key, encrypted = break_glass.encrypt_bundle(bundle)

    with pytest.raises(break_glass.BreakGlassError, match="has expired"):
        break_glass.install_bundle(
            encrypted,
            fernet_key=key,
            root=tmp_path / "expired",
            now=now + timedelta(minutes=6),
        )


def test_break_glass_bundle_rejects_wrong_key(tmp_path) -> None:
    production, backup, restore, aws = _source_files(tmp_path)
    bundle = break_glass.build_bundle(
        production_env=production,
        backup_env=backup,
        restore_env=restore,
        aws_bundle=aws,
        ttl_seconds=1800,
    )
    _key, encrypted = break_glass.encrypt_bundle(bundle)

    with pytest.raises(
        break_glass.BreakGlassError,
        match="could not be decrypted",
    ):
        break_glass.install_bundle(
            encrypted,
            fernet_key=Fernet.generate_key().decode("ascii"),
            root=tmp_path / "wrong-key",
        )


def test_break_glass_bundle_rejects_unsafe_aws_archive(tmp_path) -> None:
    production, backup, restore, aws = _source_files(tmp_path)
    _aws_bundle(aws, unsafe=True)
    bundle = break_glass.build_bundle(
        production_env=production,
        backup_env=backup,
        restore_env=restore,
        aws_bundle=aws,
        ttl_seconds=1800,
    )
    key, encrypted = break_glass.encrypt_bundle(bundle)

    with pytest.raises(
        break_glass.BreakGlassError,
        match="unsafe AWS break-glass bundle member",
    ):
        break_glass.install_bundle(
            encrypted,
            fernet_key=key,
            root=tmp_path / "unsafe",
        )

    assert not (tmp_path / "escape").exists()


def test_handoff_creation_uploads_only_ciphertext_and_presigns_get(
    tmp_path,
) -> None:
    production, backup, restore, aws = _source_files(tmp_path)

    class Client:
        def __init__(self) -> None:
            self.put = None
            self.presign = None

        def put_object(self, **kwargs):
            self.put = kwargs

        def generate_presigned_url(self, operation, *, Params, ExpiresIn):
            self.presign = (operation, Params, ExpiresIn)
            return "https://handoff.example.invalid/presigned"

    client = Client()

    class Config:
        prefix = "handoff"
        bucket = "break-glass"

        def client(self):
            return client

    result = break_glass.create_handoff(
        production_env=production,
        backup_env=backup,
        restore_env=restore,
        aws_bundle=aws,
        ttl_seconds=1800,
        config=Config(),
    )

    assert client.put["Bucket"] == "break-glass"
    assert client.put["Key"] == result["object_key"]
    ciphertext = client.put["Body"]
    assert production.read_bytes() not in ciphertext
    assert backup.read_bytes() not in ciphertext
    assert client.presign == (
        "get_object",
        {
            "Bucket": "break-glass",
            "Key": result["object_key"],
        },
        1800,
    )
    assert result["url"].startswith("https://")
    assert len(result["fernet_key"]) == 44


def test_delete_handoff_refuses_object_outside_prefix() -> None:
    class Config:
        prefix = "allowed"

        def client(self):
            raise AssertionError("client must not be called")

    with pytest.raises(
        break_glass.BreakGlassError,
        match="outside break-glass handoff prefix",
    ):
        break_glass.delete_handoff("other/object.bin", Config())


def test_bundle_metadata_contains_short_lived_expiry(tmp_path) -> None:
    production, backup, restore, aws = _source_files(tmp_path)
    now = datetime(2026, 10, 4, 2, 30, tzinfo=UTC)
    bundle = break_glass.build_bundle(
        production_env=production,
        backup_env=backup,
        restore_env=restore,
        aws_bundle=aws,
        ttl_seconds=1800,
        now=now,
    )
    payload = json.loads(bundle)
    assert payload["created_at"] == now.isoformat()
    assert payload["expires_at"] == (
        now + timedelta(minutes=30)
    ).isoformat()
