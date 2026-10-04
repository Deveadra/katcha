from __future__ import annotations

import base64
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
        info.mode = 0o600
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
        if not unsafe:
            helper = b"#!/bin/sh\nexit 0\n"
            helper_info = tarfile.TarInfo("aws_signing_helper")
            helper_info.mode = 0o755
            helper_info.size = len(helper)
            archive.addfile(helper_info, io.BytesIO(helper))


def _unsafe_aws_bytes() -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        payload = b"unsafe"
        info = tarfile.TarInfo("../escape")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


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


class _Body(io.BytesIO):
    pass


class _MemoryClient:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.deleted: list[tuple[str, str]] = []
        self.presign = None

    def put_object(self, **kwargs):
        self.objects[(kwargs["Bucket"], kwargs["Key"])] = bytes(
            kwargs["Body"]
        )

    def get_object(self, *, Bucket, Key):
        return {"Body": _Body(self.objects[(Bucket, Key)])}

    def generate_presigned_url(
        self,
        operation,
        *,
        Params,
        ExpiresIn,
    ):
        self.presign = (operation, Params, ExpiresIn)
        return "https://handoff.example.invalid/presigned"

    def delete_object(self, *, Bucket, Key):
        self.deleted.append((Bucket, Key))
        self.objects.pop((Bucket, Key), None)


class _Config:
    bucket = "break-glass"
    handoff_prefix = "bootstrap-handoff"
    escrow_prefix = "escrow"

    def __init__(self, client: _MemoryClient) -> None:
        self._client = client

    def client(self):
        return self._client


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
    assert (
        (root / "aws" / "aws_signing_helper").stat().st_mode & 0o777
    ) == 0o700


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
        match="payload could not be decrypted",
    ):
        break_glass.install_bundle(
            encrypted,
            fernet_key=Fernet.generate_key().decode("ascii"),
            root=tmp_path / "wrong-key",
        )


def test_install_rejects_unsafe_aws_archive_before_writing_secrets(
    tmp_path,
) -> None:
    production, backup, restore, aws = _source_files(tmp_path)
    bundle = json.loads(
        break_glass.build_bundle(
            production_env=production,
            backup_env=backup,
            restore_env=restore,
            aws_bundle=aws,
            ttl_seconds=1800,
        )
    )
    unsafe = _unsafe_aws_bytes()
    bundle["files"]["aws_bundle.tgz"] = {
        "base64": base64.b64encode(unsafe).decode("ascii"),
        "sha256": break_glass._sha256(unsafe),
        "size_bytes": len(unsafe),
    }
    encrypted = Fernet.generate_key()
    payload = Fernet(encrypted).encrypt(
        (json.dumps(bundle, sort_keys=True) + "\n").encode()
    )

    root = tmp_path / "unsafe"
    with pytest.raises(
        break_glass.BreakGlassError,
        match="unsafe AWS break-glass bundle member",
    ):
        break_glass.install_bundle(
            payload,
            fernet_key=encrypted.decode("ascii"),
            root=root,
        )

    assert not (root / "katcha.env").exists()
    assert not (tmp_path / "escape").exists()


def test_publish_escrow_and_rewrap_to_ephemeral_handoff(tmp_path) -> None:
    production, backup, restore, aws = _source_files(tmp_path)
    client = _MemoryClient()
    config = _Config(client)
    escrow_key = Fernet.generate_key().decode("ascii")

    escrow = break_glass.publish_escrow(
        production_env=production,
        backup_env=backup,
        restore_env=restore,
        aws_bundle=aws,
        escrow_key=escrow_key,
        config=config,
    )

    assert escrow["object_key"].startswith("escrow/")
    escrow_ciphertext = client.objects[
        ("break-glass", escrow["object_key"])
    ]
    assert production.read_bytes() not in escrow_ciphertext
    assert backup.read_bytes() not in escrow_ciphertext

    handoff = break_glass.create_handoff_from_escrow(
        escrow_object_key=escrow["object_key"],
        escrow_key=escrow_key,
        ttl_seconds=1800,
        config=config,
    )

    assert handoff["object_key"].startswith("bootstrap-handoff/")
    assert handoff["fernet_key"] != escrow_key
    assert client.presign == (
        "get_object",
        {
            "Bucket": "break-glass",
            "Key": handoff["object_key"],
        },
        1800,
    )

    installed = tmp_path / "from-escrow"
    break_glass.install_bundle(
        client.objects[("break-glass", handoff["object_key"])],
        fernet_key=str(handoff["fernet_key"]),
        root=installed,
    )
    assert (installed / "katcha.env").read_bytes() == production.read_bytes()
    assert (installed / "aws" / "config").is_file()


def test_wrong_escrow_key_cannot_create_handoff(tmp_path) -> None:
    production, backup, restore, aws = _source_files(tmp_path)
    client = _MemoryClient()
    config = _Config(client)
    escrow_key = Fernet.generate_key().decode("ascii")
    escrow = break_glass.publish_escrow(
        production_env=production,
        backup_env=backup,
        restore_env=restore,
        aws_bundle=aws,
        escrow_key=escrow_key,
        config=config,
    )

    with pytest.raises(
        break_glass.BreakGlassError,
        match="payload could not be decrypted",
    ):
        break_glass.create_handoff_from_escrow(
            escrow_object_key=escrow["object_key"],
            escrow_key=Fernet.generate_key().decode("ascii"),
            ttl_seconds=1800,
            config=config,
        )


def test_delete_commands_cannot_cross_escrow_and_handoff_prefixes() -> None:
    client = _MemoryClient()
    config = _Config(client)

    with pytest.raises(
        break_glass.BreakGlassError,
        match="invalid handoff object key",
    ):
        break_glass.delete_handoff("escrow/object.bin", config)

    with pytest.raises(
        break_glass.BreakGlassError,
        match="invalid escrow object key",
    ):
        break_glass.delete_escrow(
            "bootstrap-handoff/object.bin",
            config,
        )


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
    assert payload["kind"] == "break-glass-handoff"
    assert payload["created_at"] == now.isoformat()
    assert payload["expires_at"] == (
        now + timedelta(minutes=30)
    ).isoformat()


def test_durable_escrow_has_no_runtime_expiry(tmp_path) -> None:
    production, backup, restore, aws = _source_files(tmp_path)
    now = datetime(2026, 10, 4, 2, 30, tzinfo=UTC)
    escrow = json.loads(
        break_glass.build_escrow(
            production_env=production,
            backup_env=backup,
            restore_env=restore,
            aws_bundle=aws,
            now=now,
        )
    )

    assert escrow["kind"] == "break-glass-escrow"
    assert escrow["created_at"] == now.isoformat()
    assert "expires_at" not in escrow
