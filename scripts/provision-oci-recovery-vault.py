#!/usr/bin/env python3
"""Provision four OCI Vault recovery secrets without displaying private material.

Default: inspect administrator Vault/key metadata only (no changes).
--apply: read current OCI host files through Bastion, create or verify secrets,
then publish only their OCIDs as GitHub Actions variables.

Requires a separate OCI administrator security-token session, not the restricted
katcha-github-recovery API key. No password/key contents are logged.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

REPOSITORY = "Deveadra/katcha"
DEFAULT_VAULT_NAME = "katcha-recovery-vault"
DEFAULT_KEY_NAME = "katcha-recovery-secrets-key"
ACTIVE_DEPLOYMENT = "oci-a1-primary-001"
MAX_BUNDLE_BYTES = 24_000
SECRET_FILES = {
    "KATCHA_OCI_PRODUCTION_ENV_SECRET_ID": ("katcha-production-env", "/etc/katcha/katcha.env"),
    "KATCHA_OCI_AWS_BUNDLE_SECRET_ID": ("katcha-aws-bundle", None),
    "KATCHA_OCI_BACKUP_ENV_SECRET_ID": ("katcha-backup-env", "/etc/katcha/backup.env"),
    "KATCHA_OCI_RESTORE_ENV_SECRET_ID": ("katcha-restore-env", "/etc/katcha/restore.env"),
}
AWS_FILES = ("config", "runtime/client.pem", "runtime/client-key.pem")


class ProvisionError(Exception):
    pass


def command(args: list[str], *, sensitive: bool = False, timeout: int = 60) -> bytes:
    try:
        result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                check=False, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProvisionError(f"Command unavailable or timed out: {args[0]}") from exc
    if result.returncode:
        # Never echo CLI diagnostics; a failed secret operation might return
        # request details or sensitive content.
        raise ProvisionError(
            f"Command failed ({args[0]}, exit={result.returncode}); "
            "check the administrator session, IAM permissions and source availability"
        )
    return result.stdout


def response_json(args: list[str], *, empty_list_ok: bool = False) -> dict:
    output = command(args)
    if not output.strip():
        # OCI CLI's JSON renderer prints nothing for a successful API response
        # whose list data is []. Only the explicitly authorized list-secrets
        # metadata call may interpret this as an empty list. All other blank
        # responses remain an error; never apply this to secret-bundle reads.
        if empty_list_ok:
            return {"data": []}
        raise ProvisionError("CLI returned no JSON metadata")
    try:
        value = json.loads(output)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ProvisionError("CLI returned invalid JSON metadata") from exc
    if not isinstance(value, dict):
        raise ProvisionError("CLI returned unexpected JSON metadata")
    return value


def gh_json(*args: str) -> object:
    return json.loads(command(["gh", *args, "--repo", REPOSITORY]))


def variable_map() -> dict[str, str]:
    data = gh_json("variable", "list", "--json", "name,value")
    if not isinstance(data, list):
        raise ProvisionError("GitHub returned invalid variable metadata")
    return {entry["name"]: entry.get("value", "") for entry in data}


def unique(rows: list, name: str, label: str) -> dict:
    found = [item for item in rows if item.get("display-name") == name]
    if len(found) != 1:
        raise ProvisionError(
            f"Expected exactly one {label} named {name!r}; found {len(found)}. "
            "Do not create or choose an alternative automatically."
        )
    return found[0]


def select_vault_and_key(oci: list[str], compartment: str, vault_name: str,
                         key_name: str) -> tuple[str, str]:
    vaults = response_json(oci + [
        "kms", "management", "vault", "list", "--compartment-id", compartment, "--all",
    ]).get("data", [])
    vault = unique(vaults, vault_name, "Vault")
    if vault.get("compartment-id") != compartment or vault.get("lifecycle-state") != "ACTIVE":
        raise ProvisionError("Named Vault is not ACTIVE in the production compartment")
    if vault.get("vault-type") != "DEFAULT":
        raise ProvisionError("Named Vault is not a standard (DEFAULT) Vault")
    vault_id = vault.get("id", "")
    endpoint = vault.get("management-endpoint", "")
    if not str(vault_id).startswith("ocid1.vault.") or not str(endpoint).startswith("https://"):
        raise ProvisionError("Vault OCID or management endpoint missing")
    keys = response_json(oci + [
        "--endpoint", endpoint, "kms", "management", "key", "list",
        "--compartment-id", compartment, "--all",
    ]).get("data", [])
    key = unique(keys, key_name, "Vault key")
    if key.get("lifecycle-state") != "ENABLED" or key.get("compartment-id") != compartment:
        raise ProvisionError("Named Vault key is not ENABLED in production compartment")
    if key.get("vault-id") and key["vault-id"] != vault_id:
        raise ProvisionError("Named Vault key belongs to a different Vault")
    key_id = key.get("id", "")
    if not str(key_id).startswith("ocid1.key."):
        raise ProvisionError("Vault key OCID is invalid")
    detail = response_json(oci + [
        "--endpoint", endpoint, "kms", "management", "key", "get",
        "--key-id", key_id,
    ]).get("data", {})
    if detail.get("protection-mode") != "SOFTWARE":
        raise ProvisionError("Vault key is not SOFTWARE protected")
    if (detail.get("key-shape") or {}).get("algorithm") != "AES":
        raise ProvisionError("Vault key is not a symmetric AES key")
    if detail.get("vault-id") and detail["vault-id"] != vault_id:
        raise ProvisionError("Key details identify another Vault")
    print("VAULT_AND_SOFTWARE_AES_KEY_VERIFIED")
    return vault_id, key_id


def remote_read(path: str, home: Path) -> bytes:
    ssh = [
        "ssh", "-F", "/dev/null",
        "-i", str(home / ".ssh/katcha-oci"),
        "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={home}/.config/katcha/production/bastion/target_known_hosts",
        "-o", "ConnectTimeout=10", "-p", "22022", "ubuntu@127.0.0.1",
        "sudo", "-n", "cat", "--", path,
    ]
    return command(ssh, sensitive=True, timeout=40)


def safe_write(path: Path, data: bytes, *, limit: int = MAX_BUNDLE_BYTES) -> None:
    if not data or len(data) > limit:
        raise ProvisionError(f"Source {path.name} is empty or exceeds {limit} bytes")
    with path.open("xb") as output:
        output.write(data)
    path.chmod(0o600)


def make_stage(root: Path, home: Path, expected_epoch: int) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for variable, (_name, source) in SECRET_FILES.items():
        if source is None:
            continue
        local = root / Path(source).name
        safe_write(local, remote_read(source, home))
        paths[variable] = local

    production_text = paths["KATCHA_OCI_PRODUCTION_ENV_SECRET_ID"].read_text("utf-8")
    props = {}
    for line in production_text.splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            props[key.strip()] = value.strip().strip("'\"")
    if props.get("KATCHA_DEPLOYMENT_ID") != ACTIVE_DEPLOYMENT:
        raise ProvisionError("Hosted environment does not identify expected primary")
    if props.get("KATCHA_DEPLOYMENT_EPOCH") != str(expected_epoch):
        raise ProvisionError("Hosted epoch changed; confirm coordinator before retrying")
    if not props.get("KATCHA_LEADERSHIP_FENCE_TOKEN"):
        raise ProvisionError("Hosted runtime is missing the leadership fence credential")
    print(f"HOST_DEPLOYMENT_AND_EPOCH_VERIFIED epoch={expected_epoch}")

    aws = root / "aws"
    (aws / "runtime").mkdir(parents=True, mode=0o700)
    for relative in AWS_FILES:
        local = aws / relative
        safe_write(local, remote_read("/etc/katcha/aws/" + relative, home))
    archive = root / "aws-credentials.tgz"
    packer = Path(__file__).resolve().parent / "pack-oci-aws-vault-credentials.py"
    if not packer.is_file():
        raise ProvisionError("Cannot find reviewed AWS credential archive packer")
    command([sys.executable, str(packer), "--aws-dir", str(aws),
             "--output", str(archive)], sensitive=True)
    if not 0 < archive.stat().st_size <= MAX_BUNDLE_BYTES:
        raise ProvisionError("AWS credential archive exceeds the Vault limit")
    paths["KATCHA_OCI_AWS_BUNDLE_SECRET_ID"] = archive
    print(f"ALL_FOUR_PRIVATE_BUNDLES_STAGED aws_archive_bytes={archive.stat().st_size}")
    return paths


def existing_secret(oci: list[str], compartment: str, vault_id: str,
                    name: str) -> dict | None:
    data = response_json(oci + [
        "vault", "secret", "list", "--compartment-id", compartment,
        "--vault-id", vault_id, "--name", name, "--all", "--output", "json",
    ], empty_list_ok=True).get("data")
    if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
        raise ProvisionError("OCI secret-list response has invalid data shape")
    matches = [s for s in data if s.get("secret-name") == name]
    if len(matches) > 1:
        raise ProvisionError(f"Multiple Vault secrets named {name}; refusing ambiguity")
    return matches[0] if matches else None


def verify_content(oci: list[str], secret_id: str, staged: Path) -> None:
    record = response_json(oci + [
        "secrets", "secret-bundle", "get", "--secret-id", secret_id,
        "--stage", "CURRENT",
    ]).get("data", {})
    content = (record.get("secret-bundle-content") or {}).get("content")
    if not isinstance(content, str):
        raise ProvisionError("Secret bundle is inaccessible or has no CURRENT version")
    try:
        original = base64.b64decode(content, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ProvisionError("Existing secret content is invalid") from exc
    if hashlib.sha256(original).digest() != hashlib.sha256(staged.read_bytes()).digest():
        raise ProvisionError(
            "Existing Vault secret differs from the running OCI source; "
            "refusing replacement or automatic reuse"
        )


def wait_active(oci: list[str], secret_id: str) -> None:
    for _ in range(30):
        record = response_json(oci + [
            "vault", "secret", "get", "--secret-id", secret_id,
        ]).get("data", {})
        status = record.get("lifecycle-state")
        if status == "ACTIVE":
            return
        if status not in ("CREATING", "UPDATING"):
            raise ProvisionError(f"Vault secret entered unexpected state: {status}")
        time.sleep(5)
    raise ProvisionError("Vault secret did not become ACTIVE; inspect before retrying")


def publish_secrets(oci: list[str], compartment: str, vault_id: str, key_id: str,
                    stage: Path, paths: dict[str, Path], github: dict[str, str]) -> None:
    chosen: dict[str, str] = {}
    for variable, (name, _source) in SECRET_FILES.items():
        prior = existing_secret(oci, compartment, vault_id, name)
        if prior is not None:
            secret_id = prior.get("id", "")
            if prior.get("lifecycle-state") != "ACTIVE":
                raise ProvisionError(f"{name} is not ACTIVE; refusing to overwrite")
            verify_content(oci, secret_id, paths[variable])
            print(f"{name}: EXISTING_CONTENT_VERIFIED")
        else:
            payload = {
                "compartmentId": compartment,
                "vaultId": vault_id,
                "keyId": key_id,
                "secretName": name,
                "secretContentStage": "CURRENT",
                "secretContentContent": base64.b64encode(paths[variable].read_bytes()).decode("ascii"),
            }
            request_file = stage / "oci-secret-create-request.json"
            safe_write(request_file, json.dumps(payload).encode("utf-8"), limit=64_000)
            try:
                result = response_json(oci + [
                    "vault", "secret", "create-base64",
                    "--from-json", f"file://{request_file}",
                ])
            finally:
                request_file.unlink(missing_ok=True)
            secret_id = result.get("data", {}).get("id", "")
            print(f"{name}: CREATED")
        if not isinstance(secret_id, str) or not secret_id.startswith("ocid1.vaultsecret."):
            raise ProvisionError(f"{name} did not return a real OCI secret OCID")
        prior_gh = github.get(variable)
        if prior_gh and prior_gh != secret_id:
            raise ProvisionError(f"{variable} already points to another secret; refusing to replace")
        wait_active(oci, secret_id)
        verify_content(oci, secret_id, paths[variable])
        chosen[variable] = secret_id
        print(f"{name}: CURRENT_CONTENT_VERIFIED")

    # Do not publish partial or unverified OCI resources to the dispatcher.
    for variable, secret_id in chosen.items():
        command(["gh", "variable", "set", variable, "--repo", REPOSITORY,
                 "--body", secret_id])
        print(f"{variable}: REGISTERED")
    print("FOUR_VAULT_SECRET_OCIDS_REGISTERED")
    print("RECOVERY_CONFIGURED_UNCHANGED; WATCHDOG_UNCHANGED; PAID_FALLBACK_UNCHANGED")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--admin-profile", default="KATCHA_VAULT_ADMIN")
    parser.add_argument("--vault-name", default=DEFAULT_VAULT_NAME)
    parser.add_argument("--key-name", default=DEFAULT_KEY_NAME)
    parser.add_argument("--expected-epoch", type=int, default=4)
    options = parser.parse_args()

    home = Path.home()
    cli = home / ".cache/katcha/oci-cli-3.94.1/bin/oci"
    config = home / ".oci/config"
    if not cli.is_file() or not config.is_file():
        raise ProvisionError(
            "Missing isolated OCI CLI or administrator session config. "
            "Run 'oci session authenticate' with the administrator account first"
        )
    variables = variable_map()
    compartment = variables.get("KATCHA_OCI_COMPARTMENT_ID", "")
    region = variables.get("OCI_REGION", "")
    if not compartment.startswith("ocid1.compartment.") or not region:
        raise ProvisionError("GitHub production compartment/region is missing")
    if variables.get("KATCHA_OCI_RECOVERY_CONFIGURED", "false").lower() != "false":
        raise ProvisionError("Recovery gate is already enabled; stop before provisioning")
    if variables.get("KATCHA_OCI_PAID_FALLBACK_ENABLED", "false").lower() == "true":
        raise ProvisionError("Paid fallback must remain disabled")
    if variables.get("KATCHA_EXTERNAL_COMPUTE_ENABLED", "false").lower() == "true":
        raise ProvisionError("External-compute kill switch must remain disabled during provisioning")
    oci = [str(cli), "--config-file", str(config), "--profile",
           options.admin_profile, "--auth", "security_token", "--region", region]
    command(oci + ["session", "validate"])
    print("SEPARATE_VAULT_ADMIN_SESSION_VERIFIED")
    vault_id, key_id = select_vault_and_key(
        oci, compartment, options.vault_name, options.key_name
    )
    statuses = {}
    for variable, (name, _source) in SECRET_FILES.items():
        row = existing_secret(oci, compartment, vault_id, name)
        status = row.get("lifecycle-state", "UNKNOWN") if row else "NOT_CREATED"
        statuses[variable] = status
        print(f"{name}: {status}")
        if variables.get(variable) and (not row or variables[variable] != row.get("id")):
            raise ProvisionError(f"{variable} has a conflicting GitHub value")
    if not options.apply:
        print("INSPECT_ONLY no Vault, OCI host or GitHub changes")
        return 0
    if options.expected_epoch < 1:
        raise ProvisionError("Expected active epoch must be positive")
    old_umask = os.umask(0o077)
    try:
        with tempfile.TemporaryDirectory(prefix="katcha-oci-vault-") as temporary:
            root = Path(temporary)
            paths = make_stage(root, home, options.expected_epoch)
            publish_secrets(oci, compartment, vault_id, key_id,
                            root, paths, variables)
    finally:
        os.umask(old_umask)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ProvisionError as error:
        print(f"STOP: {error}", file=sys.stderr)
        raise SystemExit(2)
