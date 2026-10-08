"""OCI Vault must never contain the multi-megabyte public AWS helper binary."""

import os
import stat
import subprocess
import sys
import tarfile
from pathlib import Path

PACKER = Path("scripts/pack-oci-aws-vault-credentials.py")
INSTALLER = Path("deploy/scripts/install-aws-signing-helper.sh")
BOOTSTRAP = Path("deploy/cloud-init/oci-recovery-candidate.sh.tmpl")


def _aws_source(tmp_path: Path) -> Path:
    root = tmp_path / "aws"
    (root / "runtime").mkdir(parents=True)
    (root / "config").write_text("[profile katcha-automation]\n", encoding="utf-8")
    (root / "runtime/client.pem").write_text("test-certificate\n", encoding="utf-8")
    (root / "runtime/client-key.pem").write_text("test-private-key\n", encoding="utf-8")
    (root / "aws_signing_helper").write_bytes(b"a" * 4_013_211)
    return root


def _pack(root: Path, destination: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(PACKER),
            "--aws-dir",
            str(root),
            "--output",
            str(destination),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_vault_aws_bundle_contains_only_three_credential_files(tmp_path: Path) -> None:
    root = _aws_source(tmp_path)
    destination = tmp_path / "bundle.tgz"

    result = _pack(root, destination)

    assert result.returncode == 0, result.stderr
    assert "AWS_VAULT_CREDENTIAL_BUNDLE_READY" in result.stdout
    assert 0 < destination.stat().st_size <= 24_000
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    with tarfile.open(destination, "r:gz") as archive:
        assert {item.name for item in archive.getmembers()} == {
            "config", "runtime/client.pem", "runtime/client-key.pem"
        }
        assert all(item.isfile() for item in archive.getmembers())

    second = _pack(root, destination)
    assert second.returncode != 0
    assert destination.is_file()


def test_vault_aws_bundle_rejects_symlinked_key(tmp_path: Path) -> None:
    root = _aws_source(tmp_path)
    (root / "runtime/client-key.pem").unlink()
    (root / "runtime/client-key.pem").symlink_to(root / "config")

    result = _pack(root, tmp_path / "bundle.tgz")

    assert result.returncode != 0
    assert "unsafe AWS bundle member" in result.stderr
    assert not (tmp_path / "bundle.tgz").exists()


def test_vault_aws_bundle_rejects_oversized_credentials(tmp_path: Path) -> None:
    root = _aws_source(tmp_path)
    (root / "config").write_bytes(os.urandom(32_000))

    result = _pack(root, tmp_path / "bundle.tgz")

    assert result.returncode != 0
    assert "safety ceiling" in result.stderr
    assert not (tmp_path / "bundle.tgz").exists()


def test_public_helper_download_fails_closed_on_bad_sha(tmp_path: Path) -> None:
    destination = tmp_path / "aws"
    destination.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_curl = bin_dir / "curl"
    fake_curl.write_text(
        '#!/usr/bin/env bash\n'
        'while [[ "$#" -gt 0 ]]; do\n'
        '  if [[ "$1" == "--output" ]]; then\n'
        '    shift\n'
        '    printf "tampered-binary" > "$1"\n'
        '  fi\n'
        '  shift\n'
        'done\n',
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    result = subprocess.run(
        ["bash", str(INSTALLER), str(destination)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 23, result.stderr
    assert "SHA-256 mismatch" in result.stderr
    assert not (destination / "aws_signing_helper").exists()
    assert list(destination.iterdir()) == []


def test_recovery_fetches_pinned_helper_only_for_vault_mode() -> None:
    bootstrap = BOOTSTRAP.read_text(encoding="utf-8")
    installer = INSTALLER.read_text(encoding="utf-8")
    vault_branch = bootstrap.split("  oci-vault)", 1)[1].split("  break-glass)", 1)[0]
    break_glass_branch = bootstrap.split("  break-glass)", 1)[1].split("  *)", 1)[0]

    assert "install-aws-signing-helper.sh /etc/katcha/aws" in vault_branch
    assert "install-aws-signing-helper.sh" not in break_glass_branch
    assert "exactly three credential files" in vault_branch
    assert "sha256sum --check --status" in installer
    assert "1.8.5" in installer
    assert "3d131aa888cd56da446f9c6bb460b1f0569f6c7edc74eae6193a2fe3928883ba" in installer
    assert "beec9ed1c492d93db809890f16713e3556353294b823c2184ad4e891f1b2b54d" in installer
