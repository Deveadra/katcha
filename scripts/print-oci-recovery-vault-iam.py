#!/usr/bin/env python3
"""Print OCI IAM statements scoped to Katcha's four existing Vault secrets.

Read-only. Prints OCI OCIDs (resource identifiers), never secret contents.
Create the defined tag, dynamic group, and IAM policies as a Vault administrator.
"""
from __future__ import annotations

import json
import subprocess
import sys

REPO = "Deveadra/katcha"
EXPECTED_NAMES = (
    "KATCHA_OCI_PRODUCTION_ENV_SECRET_ID",
    "KATCHA_OCI_AWS_BUNDLE_SECRET_ID",
    "KATCHA_OCI_BACKUP_ENV_SECRET_ID",
    "KATCHA_OCI_RESTORE_ENV_SECRET_ID",
)
NAMESPACE = "KatchaRecovery"
KEY = "Candidate"
VALUE = "true"


def render(values: dict[str, str]) -> str:
    compartment = values.get("KATCHA_OCI_COMPARTMENT_ID", "")
    if not compartment.startswith("ocid1.compartment."):
        raise ValueError("KATCHA_OCI_COMPARTMENT_ID is not a valid OCI compartment OCID")
    secrets = [values.get(name, "") for name in EXPECTED_NAMES]
    if any(not value.startswith("ocid1.vaultsecret.") for value in secrets):
        raise ValueError("All four OCI Vault secret OCID variables must be configured")
    if len(set(secrets)) != 4:
        raise ValueError("The four Vault secret OCIDs must identify distinct secrets")
    if values.get("KATCHA_OCI_PAID_FALLBACK_ENABLED", "").lower() == "true":
        raise ValueError("Paid OCI fallback is already enabled; inspect switches first")
    if values.get("KATCHA_OCI_RECOVERY_CONFIGURED", "").lower() == "true":
        raise ValueError("Automated recovery is already enabled; inspect IAM first")

    rule = (
        "All {instance.compartment.id = '" + compartment + "', "
        "tag.KatchaRecovery.Candidate.value = 'true'}"
    )
    ids = ", ".join("target.secret.id='" + value + "'" for value in secrets)
    return "\n".join((
        "=== OCI DEFINED TAG (CREATE ONCE IN ADMIN CONSOLE) ===",
        "Namespace: KatchaRecovery",
        "Key: Candidate",
        "Type: String",
        "",
        "=== DEFAULT IDENTITY DOMAIN DYNAMIC GROUP ===",
        "Name: katcha-recovery-candidates",
        "Matching rule:",
        rule,
        "",
        "=== IAM POLICY: RECOVERY SERVICE USER MAY APPLY ONLY THIS TAG NAMESPACE ===",
        "Allow group katcha-github-recovery to use tag-namespaces in tenancy "
        "where target.tag-namespace.name='KatchaRecovery'",
        "",
        "=== IAM POLICY: CANDIDATE MAY READ ONLY THESE FOUR VAULT BUNDLES ===",
        "Allow dynamic-group katcha-recovery-candidates to read secret-bundles "
        "in compartment katcha-prod where any {" + ids + "}",
        "",
        "INSPECT_ONLY no GitHub variables, OCI IAM or compute resources changed",
    ))


def main() -> int:
    cmd = [
        "gh", "variable", "list",
        "--repo", REPO, "--json", "name,value",
    ]
    completed = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if completed.returncode:
        raise ValueError("GitHub variable metadata could not be read")
    rows = json.loads(completed.stdout)
    if not isinstance(rows, list) or any(
        not isinstance(row, dict) for row in rows
    ):
        raise ValueError("GitHub variable metadata has an invalid shape")
    variables = {row["name"]: row.get("value", "") for row in rows}
    print(render(variables))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        print("STOP:", str(exc), file=sys.stderr)
        raise SystemExit(2)
