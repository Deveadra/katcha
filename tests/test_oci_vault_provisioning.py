"""Regression coverage for non-disclosing, repeatable OCI Vault provisioning."""

import base64
import importlib.util
import json
import os
from pathlib import Path

import pytest

SOURCE = Path("scripts/provision-oci-recovery-vault.py")
SPEC = importlib.util.spec_from_file_location("katcha_vault_provision", SOURCE)
assert SPEC and SPEC.loader
provision = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(provision)


def test_vault_rejects_wrong_key_protection(monkeypatch):
    def fake_response(args):
        if args[-3:] == ["--compartment-id", "ocid1.compartment.test", "--all"]:
            if "vault" in args:
                return {"data": [{
                    "display-name": "katcha-recovery-vault",
                    "id": "ocid1.vault.test",
                    "compartment-id": "ocid1.compartment.test",
                    "lifecycle-state": "ACTIVE",
                    "vault-type": "DEFAULT",
                    "management-endpoint": "https://key-mgmt.test",
                }]}
            return {"data": [{
                "display-name": "katcha-recovery-secrets-key",
                "id": "ocid1.key.test",
                "compartment-id": "ocid1.compartment.test",
                "lifecycle-state": "ENABLED",
                "vault-id": "ocid1.vault.test",
            }]}
        return {"data": {
            "id": "ocid1.key.test", "vault-id": "ocid1.vault.test",
            "protection-mode": "HSM", "key-shape": {"algorithm": "AES"},
        }}

    monkeypatch.setattr(provision, "response_json", fake_response)
    with pytest.raises(provision.ProvisionError, match="SOFTWARE"):
        provision.select_vault_and_key(
            ["oci"], "ocid1.compartment.test",
            "katcha-recovery-vault", "katcha-recovery-secrets-key"
        )


def test_source_staging_keeps_private_material_in_temporary_directory(
    tmp_path, monkeypatch, capsys
):
    fixed = {
        "/etc/katcha/katcha.env":
            b"KATCHA_DEPLOYMENT_ID=oci-a1-primary-001\n"
            b"KATCHA_DEPLOYMENT_EPOCH=4\n"
            b"KATCHA_LEADERSHIP_FENCE_TOKEN=never-print-me\n",
        "/etc/katcha/backup.env": b"KATCHA_BACKUP_R2_SECRET_KEY=private\n",
        "/etc/katcha/restore.env": b"KATCHA_RESTORE_R2_SECRET_KEY=private\n",
        "/etc/katcha/aws/config": b"[profile runtime]\nsecret=private\n",
        "/etc/katcha/aws/runtime/client.pem": b"private-certificate\n",
        "/etc/katcha/aws/runtime/client-key.pem": b"private-signing-key\n",
    }
    monkeypatch.setattr(provision, "remote_read", lambda path, home: fixed[path])
    paths = provision.make_stage(tmp_path, tmp_path, 4)
    assert len(paths) == 4
    assert 0 < paths["KATCHA_OCI_AWS_BUNDLE_SECRET_ID"].stat().st_size <= 24_000
    for name, path in paths.items():
        assert path.is_file(), name
        assert path.stat().st_mode & 0o077 == 0
    output = capsys.readouterr().out
    assert "never-print-me" not in output
    assert "private-signing-key" not in output


def test_stage_rejects_changed_epoch_before_upload(tmp_path, monkeypatch):
    monkeypatch.setattr(
        provision, "remote_read",
        lambda path, home: b"KATCHA_DEPLOYMENT_ID=oci-a1-primary-001\nKATCHA_DEPLOYMENT_EPOCH=5\n"
    )
    with pytest.raises(provision.ProvisionError, match="epoch changed"):
        provision.make_stage(tmp_path, tmp_path, 4)


def test_creates_four_secrets_using_private_file_json_not_command_line(
    tmp_path, monkeypatch, capsys
):
    staged = {}
    for name, (secret, _path) in provision.SECRET_FILES.items():
        local = tmp_path / (secret + ".txt")
        local.write_bytes(os.urandom(23_000) if "AWS_BUNDLE" in name else b"private-credential")
        staged[name] = local
    seen = {}
    ids = {}
    counter = 0

    def fake_response(args):
        nonlocal counter
        assert "--secret-content-content" not in args
        assert "private-credential" not in str(args)
        if "create-base64" in args:
            path = Path(args[-1].removeprefix("file://"))
            assert path.is_file()
            request = json.loads(path.read_text())
            decoded = base64.b64decode(request["secretContentContent"], validate=True)
            assert request["secretContentStage"] == "CURRENT"
            assert len(decoded) <= 24_000
            counter += 1
            identifier = f"ocid1.vaultsecret.test.{counter}"
            ids[request["secretName"]] = identifier
            seen[request["secretName"]] = decoded
            return {"data": {"id": identifier}}
        raise AssertionError(f"unexpected OCI query: {args}")

    monkeypatch.setattr(provision, "existing_secret", lambda *args: None)
    monkeypatch.setattr(provision, "response_json", fake_response)
    monkeypatch.setattr(provision, "wait_active", lambda *args: None)

    def verify(oci, secret_id, staged_path):
        assert any(
            name in seen and ids[name] == secret_id and seen[name] == staged_path.read_bytes()
            for name in ids
        )

    monkeypatch.setattr(provision, "verify_content", verify)
    published = []

    def fake_command(args, **kwargs):
        assert args[:3] == ["gh", "variable", "set"]
        assert args[-2] == "--body"
        assert args[-1].startswith("ocid1.vaultsecret.")
        published.append(args[3])
        return b""

    monkeypatch.setattr(provision, "command", fake_command)
    provision.publish_secrets(
        ["oci"], "ocid1.compartment.test", "ocid1.vault.test",
        "ocid1.key.test", tmp_path, staged, {}
    )
    assert len(published) == len(provision.SECRET_FILES) == 4
    assert not (tmp_path / "oci-secret-create-request.json").exists()
    output = capsys.readouterr().out
    assert "private-credential" not in output
    assert "FOUR_VAULT_SECRET_OCIDS_REGISTERED" in output


def test_partial_failure_never_registers_github_variables(
    tmp_path, monkeypatch
):
    staged = {}
    for variable, (name, _) in provision.SECRET_FILES.items():
        path = tmp_path / name
        path.write_bytes(b"secrets")
        staged[variable] = path
    index = 0

    def fake_response(args):
        nonlocal index
        index += 1
        return {"data": {"id": f"ocid1.vaultsecret.test.{index}"}}

    monkeypatch.setattr(provision, "existing_secret", lambda *args: None)
    monkeypatch.setattr(provision, "response_json", fake_response)
    monkeypatch.setattr(provision, "wait_active", lambda *args: None)
    monkeypatch.setattr(
        provision, "verify_content",
        lambda *args: (_ for _ in ()).throw(provision.ProvisionError("read-back mismatch"))
    )
    published = []
    monkeypatch.setattr(provision, "command", lambda args, **kw: published.append(args))
    with pytest.raises(provision.ProvisionError, match="read-back mismatch"):
        provision.publish_secrets(
            ["oci"], "ocid1.compartment.test", "ocid1.vault.test",
            "ocid1.key.test", tmp_path, staged, {}
        )
    assert published == []


def test_empty_successful_oci_secret_listing_is_safe_to_treat_as_no_secrets(monkeypatch):
    calls = []

    def no_secrets(args, **kwargs):
        calls.append(args)
        return b""

    monkeypatch.setattr(provision, "command", no_secrets)
    row = provision.existing_secret(
        ["oci"], "ocid1.compartment.test", "ocid1.vault.test", "katcha-aws-bundle"
    )
    assert row is None
    assert len(calls) == 1
    assert calls[0][-2:] == ["--output", "json"]
    assert "list" in calls[0] and "--name" in calls[0]


def test_empty_other_oci_metadata_remains_a_hard_error(monkeypatch):
    monkeypatch.setattr(provision, "command", lambda args, **kwargs: b"")
    with pytest.raises(provision.ProvisionError, match="no JSON metadata"):
        provision.response_json(["oci", "vault", "secret", "get"])


def test_secret_list_invalid_or_unexpected_shape_never_looks_empty(monkeypatch):
    responses = [
        b"not JSON",
        b"{}",
        b'{"data":null}',
        b'{"data":{}}',
        b'{"data":[null]}',
        b'{"data":"not a list"}',
    ]
    for raw in responses:
        monkeypatch.setattr(provision, "command", lambda args, **kwargs: raw)
        with pytest.raises(provision.ProvisionError):
            provision.existing_secret(
                ["oci"], "ocid1.compartment.test", "ocid1.vault.test",
                "katcha-production-env"
            )


def test_populated_secret_list_resolves_exact_name(monkeypatch):
    existing = {
        "secret-name": "katcha-backup-env",
        "id": "ocid1.vaultsecret.test.example",
        "lifecycle-state": "ACTIVE",
    }
    monkeypatch.setattr(
        provision, "command",
        lambda args, **kwargs: json.dumps({"data": [existing]}).encode("utf-8"),
    )
    assert provision.existing_secret(
        ["oci"], "ocid1.compartment.test", "ocid1.vault.test", "katcha-backup-env"
    ) == existing


def test_cli_failure_does_not_get_interpreted_as_empty_secret_list(monkeypatch):
    def access_denied(_args, **_kwargs):
        raise provision.ProvisionError("Command failed (oci, exit=1)")

    monkeypatch.setattr(provision, "command", access_denied)
    with pytest.raises(provision.ProvisionError, match="exit=1"):
        provision.existing_secret(
            ["oci"], "ocid1.compartment.test", "ocid1.vault.test",
            "katcha-restore-env"
        )
