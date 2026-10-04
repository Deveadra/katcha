from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from katcha.ops import breakglass_bundle


def _sources(tmp_path: Path) -> breakglass_bundle.BundleInputs:
    production = tmp_path / "katcha.env"
    backup = tmp_path / "backup.env"
    restore = tmp_path / "restore.env"
    aws_bundle = tmp_path / "aws.tgz"

    production.write_text(
        "KATCHA_ENV=production\n"
        "KATCHA_CREDENTIAL_ENCRYPTION_KEY=test-key-material\n",
        encoding="utf-8",
    )
    backup.write_text(
        "KATCHA_BACKUP_R2_ACCESS_KEY=writer\n",
        encoding="utf-8",
    )
    restore.write_text(
        "KATCHA_RESTORE_R2_ACCESS_KEY=reader\n",
        encoding="utf-8",
    )
    aws_bundle.write_bytes(b"fixture-aws-bundle")

    return breakglass_bundle.BundleInputs(
        production_env=production,
        backup_env=backup,
        restore_env=restore,
        aws_bundle=aws_bundle,
    )


def _plain_archive(tmp_path: Path) -> Path:
    inputs = _sources(tmp_path)
    payloads = {
        name: path.read_bytes()
        for name, path in inputs.payloads().items()
    }
    manifest = breakglass_bundle._manifest_for(payloads)
    archive = tmp_path / "bundle.tar"
    breakglass_bundle._write_plain_archive(
        archive,
        payloads=payloads,
        manifest=manifest,
    )
    return archive


def test_plain_archive_manifest_verifies_expected_payloads(tmp_path: Path) -> None:
    archive = _plain_archive(tmp_path)

    manifest = breakglass_bundle.inspect_plain_archive(archive)

    assert manifest["format_version"] == 1
    assert manifest["payload_count"] == 4
    assert {
        row["name"]
        for row in manifest["payloads"]
    } == breakglass_bundle.EXPECTED_PAYLOADS


def test_plain_archive_rejects_unexpected_tar_entry(tmp_path: Path) -> None:
    archive = _plain_archive(tmp_path)
    malicious = tmp_path / "malicious.tar"

    with tarfile.open(archive, mode="r") as source, tarfile.open(
        malicious,
        mode="w",
    ) as target:
        for member in source.getmembers():
            data = source.extractfile(member)
            target.addfile(member, data)
        payload = b"unexpected"
        info = tarfile.TarInfo("extra-secret.txt")
        info.size = len(payload)
        target.addfile(info, io.BytesIO(payload))

    with pytest.raises(
        breakglass_bundle.BreakGlassError,
        match="entry set is invalid",
    ):
        breakglass_bundle.inspect_plain_archive(malicious)


def test_plain_archive_rejects_path_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "traversal.tar"
    with tarfile.open(archive, mode="w") as target:
        payload = b"nope"
        info = tarfile.TarInfo("../escape")
        info.size = len(payload)
        target.addfile(info, io.BytesIO(payload))

    with pytest.raises(
        breakglass_bundle.BreakGlassError,
        match="unsafe path",
    ):
        breakglass_bundle.inspect_plain_archive(archive)


def test_plain_archive_rejects_tampered_payload(tmp_path: Path) -> None:
    source = _plain_archive(tmp_path)
    tampered = tmp_path / "tampered.tar"

    with tarfile.open(source, mode="r") as original, tarfile.open(
        tampered,
        mode="w",
    ) as target:
        for member in original.getmembers():
            data = original.extractfile(member)
            assert data is not None
            raw = data.read()
            if member.name == "katcha.env":
                raw += b"tampered=true\n"
                member = tarfile.TarInfo(member.name)
                member.size = len(raw)
            target.addfile(member, io.BytesIO(raw))

    with pytest.raises(
        breakglass_bundle.BreakGlassError,
        match="size does not match manifest|checksum does not match manifest",
    ):
        breakglass_bundle.inspect_plain_archive(tampered)


def test_create_rejects_symlinked_secret_source(tmp_path: Path) -> None:
    inputs = _sources(tmp_path)
    link = tmp_path / "production-link.env"
    link.symlink_to(inputs.production_env)
    recipients = tmp_path / "recipients.txt"
    recipients.write_text(
        "age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq\n",
        encoding="utf-8",
    )

    with pytest.raises(
        breakglass_bundle.BreakGlassError,
        match="must not be a symlink",
    ):
        breakglass_bundle.create_bundle(
            breakglass_bundle.BundleInputs(
                production_env=link,
                backup_env=inputs.backup_env,
                restore_env=inputs.restore_env,
                aws_bundle=inputs.aws_bundle,
            ),
            recipients_file=recipients,
            output=tmp_path / "bundle.age",
        )


@pytest.mark.skipif(
    shutil.which("age") is None or shutil.which("age-keygen") is None,
    reason="age binaries are not installed",
)
def test_age_roundtrip_extracts_only_verified_payloads(tmp_path: Path) -> None:
    inputs = _sources(tmp_path)
    identity = tmp_path / "breakglass.identity"
    subprocess.run(
        ["age-keygen", "-o", str(identity)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    recipient = subprocess.run(
        ["age-keygen", "-y", str(identity)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    recipients = tmp_path / "recipients.txt"
    recipients.write_text(recipient + "\n", encoding="utf-8")

    bundle = tmp_path / "katcha-breakglass.age"
    breakglass_bundle.create_bundle(
        inputs,
        recipients_file=recipients,
        output=bundle,
    )

    assert bundle.is_file()
    assert stat_mode(bundle) == 0o600
    assert b"KATCHA_CREDENTIAL_ENCRYPTION_KEY" not in bundle.read_bytes()

    destination = tmp_path / "extracted"
    breakglass_bundle.extract_bundle(
        bundle,
        identity_file=identity,
        destination=destination,
    )

    assert set(path.name for path in destination.iterdir()) == {
        *breakglass_bundle.EXPECTED_PAYLOADS,
        breakglass_bundle.MANIFEST_NAME,
    }
    for name, source in inputs.payloads().items():
        target = destination / name
        assert target.read_bytes() == source.read_bytes()
        assert stat_mode(target) == 0o600
    assert stat_mode(destination) == 0o700

    manifest = json.loads(
        (destination / breakglass_bundle.MANIFEST_NAME).read_text(
            encoding="utf-8"
        )
    )
    assert manifest["payload_count"] == 4


@pytest.mark.skipif(
    shutil.which("age") is None or shutil.which("age-keygen") is None,
    reason="age binaries are not installed",
)
def test_wrong_age_identity_cannot_extract_bundle(tmp_path: Path) -> None:
    inputs = _sources(tmp_path)
    identity = tmp_path / "good.identity"
    wrong_identity = tmp_path / "wrong.identity"

    for path in (identity, wrong_identity):
        subprocess.run(
            ["age-keygen", "-o", str(path)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    recipient = subprocess.run(
        ["age-keygen", "-y", str(identity)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    recipients = tmp_path / "recipients.txt"
    recipients.write_text(recipient + "\n", encoding="utf-8")

    bundle = tmp_path / "bundle.age"
    breakglass_bundle.create_bundle(
        inputs,
        recipients_file=recipients,
        output=bundle,
    )

    with pytest.raises(
        breakglass_bundle.BreakGlassError,
        match="decryption failed",
    ):
        breakglass_bundle.extract_bundle(
            bundle,
            identity_file=wrong_identity,
            destination=tmp_path / "wrong-output",
        )


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777
