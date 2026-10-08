#!/usr/bin/env python3
"""Read-only OCI IAM and production backup/restore readiness evidence.

No secret-bundle retrieval, OCI mutation, recovery dispatch, timer triggers,
container starts, or Git operations. Only the four Vault secret *identifiers*
are inspected. Results are intentionally non-secret and can be shared.
"""
from __future__ import annotations

import argparse
import configparser
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = "Deveadra/katcha"
VAULT_VARS = (
    "KATCHA_OCI_PRODUCTION_ENV_SECRET_ID",
    "KATCHA_OCI_AWS_BUNDLE_SECRET_ID",
    "KATCHA_OCI_BACKUP_ENV_SECRET_ID",
    "KATCHA_OCI_RESTORE_ENV_SECRET_ID",
)
EXPECTED_POLICY_NAME = "katcha-recovery-vault-access"
EXPECTED_GROUP_NAME = "katcha-recovery-candidates"
REMOTE_PY = r'''
import datetime
import json
import re
import subprocess
from pathlib import Path

UNITS = ("katcha-backup", "katcha-restore-test")
PATTERNS = {
    "katcha-backup": re.compile(
        r"^Katcha PostgreSQL backup completed: [0-9]{8}T[0-9]{6}Z-[0-9a-f-]{36}$"
    ),
    "katcha-restore-test": re.compile(
        r"^Katcha disaster restore test passed for [0-9]+ databases\.$"
    ),
}

def run(*args):
    result = subprocess.run(
        list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, check=False, timeout=45
    )
    if result.returncode != 0:
        raise RuntimeError("read-only system command failed")
    return result.stdout

report = {}
report["durable_mount"] = (
    subprocess.run(
        ["mountpoint", "-q", "/srv/katcha"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False
    ).returncode == 0
)
for unit in UNITS:
    timer = unit + ".timer"
    service = unit + ".service"
    show = run(
        "systemctl", "show", timer,
        "-p", "LoadState", "-p", "ActiveState", "-p", "UnitFileState",
        "-p", "NextElapseUSecRealtime", "--no-pager"
    )
    timer_props = dict(
        line.split("=", 1) for line in show.splitlines() if "=" in line
    )
    service_show = run(
        "systemctl", "show", service,
        "-p", "LoadState", "-p", "Result", "-p", "ExecMainStatus",
        "--no-pager"
    )
    service_props = dict(
        line.split("=", 1) for line in service_show.splitlines() if "=" in line
    )
    entries = run(
        "journalctl", "--unit", service, "--since", "14 days ago",
        "--output", "json", "--no-pager", "-q"
    )
    successes = []
    for line in entries.splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = entry.get("MESSAGE", "")
        if not isinstance(message, str) or not PATTERNS[unit].fullmatch(message):
            continue
        try:
            when = datetime.datetime.fromtimestamp(
                int(entry["__REALTIME_TIMESTAMP"]) / 1_000_000,
                tz=datetime.timezone.utc
            )
        except (ValueError, KeyError, TypeError, OverflowError):
            continue
        successes.append(when)
    report[unit] = {
        "timer_active": timer_props.get("ActiveState") == "active",
        "timer_enabled": timer_props.get("UnitFileState") == "enabled",
        "timer_loaded": timer_props.get("LoadState") == "loaded",
        "next_trigger": timer_props.get("NextElapseUSecRealtime") or "unknown",
        "service_result": service_props.get("Result") or "unknown",
        "service_exec_status": service_props.get("ExecMainStatus") or "unknown",
        "last_success_utc": max(successes).isoformat() if successes else None,
    }
print(json.dumps(report, separators=(",", ":")))
'''


class AuditError(Exception):
    pass


def read_command(argv: list[str], *, stdin: str | None = None, timeout: int = 80) -> str:
    try:
        completed = subprocess.run(
            argv, input=stdin, capture_output=True, text=True,
            check=False, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AuditError("read-only command unavailable or timed out") from exc
    if completed.returncode != 0:
        # Deliberately never echo CLI or remote stderr, which may expose data.
        raise AuditError(
            "read-only command failed (exit " + str(completed.returncode) + ")"
        )
    return completed.stdout


def metadata(argv: list[str], *, empty_list_ok: bool = False) -> dict:
    raw = read_command(argv)
    if not raw.strip():
        if empty_list_ok:
            return {"data": []}
        raise AuditError("OCI returned no JSON metadata")
    try:
        response = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AuditError("OCI returned invalid JSON metadata") from exc
    if not isinstance(response, dict):
        raise AuditError("OCI returned unexpected metadata shape")
    return response


def github_vars() -> dict[str, str]:
    raw = read_command(["gh", "variable", "list", "--repo", REPO, "--json", "name,value"])
    try:
        rows = json.loads(raw)
        if not isinstance(rows, list) or any(
            not isinstance(row, dict) or "name" not in row for row in rows
        ):
            raise ValueError("bad shape")
    except (json.JSONDecodeError, ValueError) as exc:
        raise AuditError("GitHub variable metadata is invalid") from exc
    return {row["name"]: row.get("value", "") for row in rows}


def compact(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


def policy_matches(statements: list[str], variables: dict[str, str]) -> tuple[bool, bool]:
    norm = {compact(statement) for statement in statements if isinstance(statement, str)}
    expected_tag = (
        "Allow group katcha-github-recovery to use tag-namespaces in tenancy "
        "where target.tag-namespace.name='KatchaRecovery'"
    )
    ids = [variables.get(name, "") for name in VAULT_VARS]
    # OCI may preserve a different order for an 'any' condition; check its
    # fixed prefix and the *exact* four target.secret.id values instead.
    found_tag = compact(expected_tag) in norm
    vault_prefix = compact(
        "Allow dynamic-group katcha-recovery-candidates to read secret-bundles "
        "in compartment katcha-prod where any {"
    )
    expected_conditions = {
        "target.secret.id='" + secret_id.lower() + "'" for secret_id in ids
    }
    found_vault = any(
        entry.startswith(vault_prefix)
        and entry.endswith("}")
        and {
            part for part in entry[len(vault_prefix):-1].split(",") if part
        } == expected_conditions
        and len(entry[len(vault_prefix):-1].split(",")) == 4
        for entry in norm
    )
    return found_tag, found_vault


def iam_audit(oci: list[str], tenancy: str, variables: dict[str, str]) -> list[tuple[str, bool]]:
    production = variables.get("KATCHA_OCI_COMPARTMENT_ID", "")
    if not production.startswith("ocid1.compartment."):
        raise AuditError("production compartment OCID is absent")
    results: list[tuple[str, bool]] = []
    domains = metadata(
        oci + ["iam", "domain", "list", "--compartment-id", tenancy, "--all", "--output", "json"],
        empty_list_ok=True,
    ).get("data", [])
    default = [
        row for row in domains if isinstance(row, dict)
        and row.get("display-name") == "Default"
    ]
    if len(default) != 1 or not str(default[0].get("url", "")).startswith("https://"):
        raise AuditError("Default identity-domain URL was not uniquely returned")
    domain_url = default[0]["url"]

    groups = metadata(
        oci + ["--endpoint", domain_url, "identity-domains",
               "dynamic-resource-groups", "list", "--all", "--output", "json"],
    ).get("data")
    resources = groups.get("Resources", groups.get("resources")) if isinstance(groups, dict) else None
    if not isinstance(resources, list):
        raise AuditError("Default identity-domain dynamic group listing has unknown shape")
    matching = [
        row for row in resources if isinstance(row, dict)
        and row.get("displayName", row.get("display-name")) == EXPECTED_GROUP_NAME
    ]
    expected = (
        f"All {{instance.compartment.id = '{production}', "
        "tag.KatchaRecovery.Candidate.value = 'true'}"
    )
    actual_rule = matching[0].get("matchingRule", matching[0].get("matching-rule", "")) \
        if len(matching) == 1 else ""
    results.append((
        "default_domain_dynamic_group_matching_rule",
        len(matching) == 1 and compact(actual_rule) == compact(expected),
    ))

    namespaces = metadata(
        oci + ["iam", "tag-namespace", "list", "--compartment-id", tenancy,
               "--all", "--output", "json"], empty_list_ok=True,
    ).get("data", [])
    namespace = [
        row for row in namespaces if isinstance(row, dict)
        and row.get("name") == "KatchaRecovery"
        and row.get("lifecycle-state") == "ACTIVE"
    ]
    results.append(("active_defined_tag_namespace", len(namespace) == 1))
    if len(namespace) == 1:
        tags = metadata(
            oci + ["iam", "tag", "list", "--tag-namespace-id",
                   namespace[0]["id"], "--all", "--output", "json"],
            empty_list_ok=True,
        ).get("data", [])
        results.append((
            "candidate_tag_key",
            sum(
                isinstance(row, dict)
                and row.get("name") == "Candidate"
                and row.get("lifecycle-state") == "ACTIVE"
                for row in tags
            ) == 1,
        ))

    policies = metadata(
        oci + ["iam", "policy", "list", "--compartment-id", tenancy,
               "--all", "--output", "json"],
        empty_list_ok=True,
    ).get("data", [])
    named = [
        row for row in policies if isinstance(row, dict)
        and row.get("name") == EXPECTED_POLICY_NAME
        and row.get("lifecycle-state") == "ACTIVE"
    ]
    results.append(("active_root_iam_policy", len(named) == 1))
    if len(named) == 1:
        policy = metadata(
            oci + ["iam", "policy", "get", "--policy-id",
                   named[0]["id"], "--output", "json"],
        ).get("data", {})
        statements = policy.get("statements", []) if isinstance(policy, dict) else []
        tag_ok, secrets_ok = policy_matches(statements, variables)
        results.append(("github_group_defined_tag_grant", tag_ok))
        results.append(("candidate_four_secret_only_grant", secrets_ok))

    for variable in VAULT_VARS:
        secret_id = variables.get(variable, "")
        if not secret_id.startswith("ocid1.vaultsecret."):
            results.append((variable + "_metadata_active", False))
            continue
        try:
            secret = metadata(oci + [
                "vault", "secret", "get", "--secret-id", secret_id, "--output", "json"
            ]).get("data", {})
            passed = (
                isinstance(secret, dict)
                and secret.get("lifecycle-state") == "ACTIVE"
                and secret.get("compartment-id") == production
            )
        except AuditError:
            passed = False
        results.append((variable + "_metadata_active", passed))

    primary_id = variables.get("KATCHA_OCI_PRIMARY_INSTANCE_ID", "")
    if primary_id.startswith("ocid1.instance."):
        primary = metadata(oci + [
            "compute", "instance", "get", "--instance-id", primary_id, "--output", "json"
        ]).get("data", {})
        tags = primary.get("defined-tags") or {} if isinstance(primary, dict) else {}
        candidate = tags.get("KatchaRecovery") if isinstance(tags, dict) else {}
        results.append((
            "active_primary_has_no_candidate_tag",
            not isinstance(candidate, dict) or candidate.get("Candidate") != "true",
        ))

    return results


def remote_backup_audit(home: Path) -> dict:
    ssh = [
        "ssh", "-F", "/dev/null", "-i", str(home / ".ssh/katcha-oci"),
        "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={home}/.config/katcha/production/bastion/target_known_hosts",
        "-o", "ConnectTimeout=10", "-p", "22022", "ubuntu@127.0.0.1",
        "sudo", "-n", "python3", "-",
    ]
    result = read_command(ssh, stdin=REMOTE_PY, timeout=150)
    try:
        value = json.loads(result)
    except json.JSONDecodeError as exc:
        raise AuditError("OCI host did not return valid filtered audit JSON") from exc
    if not isinstance(value, dict):
        raise AuditError("OCI host returned an invalid audit report")
    return value


def print_backup_report(data: dict) -> None:
    print("durable_mount:", "PASS" if data.get("durable_mount") is True else "UNVERIFIED")
    for unit in ("katcha-backup", "katcha-restore-test"):
        row = data.get(unit, {})
        if not isinstance(row, dict):
            print(unit + ": UNVERIFIED (report missing)")
            continue
        print(
            unit + ": "
            + ("TIMER_ACTIVE_ENABLED" if (
                row.get("timer_active") is True and row.get("timer_enabled") is True
                and row.get("timer_loaded") is True
            ) else "TIMER_NOT_READY")
            + " last_success_utc=" + str(row.get("last_success_utc") or "NONE")
            + " service_result=" + str(row.get("service_result") or "UNKNOWN")
        )
        print(unit + "_next_trigger: " + str(row.get("next_trigger") or "unknown"))
    print("NOTE: backup success markers indicate completed R2 uploads; restore "
          "markers prove an isolated PostgreSQL restore. Neither proves "
          "an actual replacement instance can read Vault bundles.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iam-only", action="store_true")
    parser.add_argument("--backups-only", action="store_true")
    parser.add_argument("--admin-profile", default="KATCHA_VAULT_ADMIN")
    options = parser.parse_args()
    if options.iam_only and options.backups_only:
        raise AuditError("select at most one audit scope")
    do_iam = not options.backups_only
    do_backup = not options.iam_only

    home = Path.home()
    variables = github_vars()
    print("=== GITHUB RECOVERY SAFETY GATES ===")
    for key in ("KATCHA_OCI_RECOVERY_CONFIGURED", "KATCHA_EXTERNAL_COMPUTE_ENABLED",
                "KATCHA_OCI_PAID_FALLBACK_ENABLED"):
        value = variables.get(key, "MISSING").lower()
        print(key + ": " + ("SAFE_FALSE" if value == "false" else "REVIEW_" + value))
    results: list[tuple[str, bool]] = []

    if do_iam:
        print("\n=== OCI IAM METADATA (READ ONLY) ===")
        cli = home / ".cache/katcha/oci-cli-3.94.1/bin/oci"
        config_file = home / ".oci/config"
        config = configparser.ConfigParser(interpolation=None)
        if not cli.is_file() or not config.read(config_file):
            raise AuditError("isolated OCI CLI or administrator session config not found")
        if options.admin_profile not in config:
            raise AuditError("OCI administrator profile is not in the local CLI config")
        tenancy = config[options.admin_profile].get("tenancy", "")
        if not tenancy.startswith("ocid1.tenancy."):
            raise AuditError("OCI administrator session has no tenancy OCID")
        oci = [str(cli), "--config-file", str(config_file), "--profile",
               options.admin_profile, "--auth", "security_token", "--region",
               variables.get("OCI_REGION", "us-ashburn-1")]
        try:
            read_command(oci + ["session", "validate"])
        except AuditError as exc:
            raise AuditError(
                "Administrator session expired. Reauthenticate in the OCI browser "
                "and rerun read-only inspection"
            ) from exc
        print("vault_admin_session: VALID")
        results = iam_audit(oci, tenancy, variables)
        for name, passed in results:
            print(name + ": " + ("PASS" if passed else "NOT_VERIFIED"))

    if do_backup:
        print("\n=== OCI HOST BACKUP/RESTORE EVIDENCE (READ ONLY) ===")
        try:
            print_backup_report(remote_backup_audit(home))
        except AuditError:
            print(
                "HOST_AUDIT_UNAVAILABLE: Bastion must already listen on "
                "127.0.0.1:22022; no connection or service changes attempted"
            )

    print("\nREAD_ONLY_AUDIT_FINISHED")
    if do_iam and results and not all(value for _, value in results):
        print("IAM_METADATA_REQUIRES_REVIEW")
    print("LIVE_INSTANCE_PRINCIPAL_RECOVERY_NOT_TESTED")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AuditError as exc:
        print("STOP:", str(exc), file=sys.stderr)
        raise SystemExit(2)
