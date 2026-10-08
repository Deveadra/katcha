"""Recovery readiness audits must stay non-mutating and fail closed on policy scope."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SOURCE = Path("scripts/audit-oci-recovery-readiness.py")
SPEC = importlib.util.spec_from_file_location("katcha_oci_readonly_audit", SOURCE)
assert SPEC and SPEC.loader
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


def _variables() -> dict[str, str]:
    return {
        "KATCHA_OCI_COMPARTMENT_ID": "ocid1.compartment.test",
        "KATCHA_OCI_PRODUCTION_ENV_SECRET_ID": "ocid1.vaultsecret.test.production",
        "KATCHA_OCI_AWS_BUNDLE_SECRET_ID": "ocid1.vaultsecret.test.aws",
        "KATCHA_OCI_BACKUP_ENV_SECRET_ID": "ocid1.vaultsecret.test.backup",
        "KATCHA_OCI_RESTORE_ENV_SECRET_ID": "ocid1.vaultsecret.test.restore",
    }


def _grant() -> list[str]:
    v = _variables()
    tag = (
        "Allow group katcha-github-recovery to use tag-namespaces in tenancy "
        "where target.tag-namespace.name='KatchaRecovery'"
    )
    vault = (
        "Allow dynamic-group katcha-recovery-candidates to read secret-bundles "
        "in compartment katcha-prod where any {"
        + ", ".join(
            f"target.secret.id='{v[name]}'" for name in audit.VAULT_VARS
        )
        + "}"
    )
    return [tag, vault]


def test_exact_iam_grants_are_accepted_even_with_reordered_conditions():
    variables = _variables()
    tag, vault = _grant()
    assert audit.policy_matches([tag, vault], variables) == (True, True)
    leading, body = vault.split("where any {", 1)
    tokens = body.removesuffix("}").split(", ")
    reordered = leading + "where any {" + ", ".join(reversed(tokens)) + "}"
    assert audit.policy_matches([tag, reordered], variables) == (True, True)


@pytest.mark.parametrize(
    "modified",
    [
        "Allow dynamic-group katcha-recovery-candidates to read secret-bundles "
        "in compartment katcha-prod",
        "Allow dynamic-group katcha-recovery-candidates to read secret-bundles "
        "in compartment katcha-prod where any {target.secret.id='other'}",
        "Allow dynamic-group katcha-recovery-candidates to manage secret-family "
        "in compartment katcha-prod",
        "Allow dynamic-group katcha-recovery-candidates to read secret-bundles "
        "in tenancy",
    ],
)
def test_broad_or_incorrect_candidate_grants_rejected(modified):
    assert audit.policy_matches([_grant()[0], modified], _variables()) == (True, False)


def test_empty_or_invalid_oci_metadata_is_not_treated_as_valid(monkeypatch):
    monkeypatch.setattr(audit, "read_command", lambda _argv: b"".decode())
    with pytest.raises(audit.AuditError, match="no JSON metadata"):
        audit.metadata(["oci", "iam", "policy", "get"])
    assert audit.metadata(["oci", "iam", "policy", "list"], empty_list_ok=True) == {
        "data": []
    }

    monkeypatch.setattr(audit, "read_command", lambda _argv: '{"data": null}')
    assert audit.metadata(["oci", "iam", "policy", "list"])["data"] is None


def test_remote_host_audit_runs_only_filtered_readonly_python(monkeypatch, tmp_path):
    called = []

    def fake_read(argv, *, stdin=None, timeout=80):
        called.append((argv, stdin, timeout))
        return json.dumps({
            "durable_mount": True,
            "katcha-backup": {
                "timer_active": True,
                "timer_enabled": True,
                "timer_loaded": True,
                "last_success_utc": "2026-10-08T02:00:00+00:00",
                "service_result": "success",
            },
        })

    monkeypatch.setattr(audit, "read_command", fake_read)
    result = audit.remote_backup_audit(tmp_path)
    assert result["durable_mount"] is True
    argv, stdin, _ = called[0]
    assert argv[-3:] == ["sudo", "-n", "python3", "-"][-3:]
    assert "-p" in argv and argv[argv.index("-p") + 1] == "22022"
    assert "systemctl" in stdin and "journalctl" in stdin
    assert "systemctl start" not in stdin
    assert "secret-bundle get" not in stdin
    assert "docker run" not in stdin
    assert "rm -rf" not in stdin


def test_backup_report_redacts_unfiltered_journals(capsys):
    audit.print_backup_report({
        "durable_mount": True,
        "katcha-backup": {
            "timer_active": True,
            "timer_enabled": True,
            "timer_loaded": True,
            "last_success_utc": "2026-10-08T01:00:00+00:00",
            "service_result": "success",
        },
        "katcha-restore-test": {
            "timer_active": True,
            "timer_enabled": True,
            "timer_loaded": True,
            "last_success_utc": None,
            "service_result": "success",
        },
    })
    text = capsys.readouterr().out
    assert "backup: TIMER_ACTIVE_ENABLED" in text
    assert "restore-test: TIMER_ACTIVE_ENABLED" in text
    assert "last_success_utc=NONE" in text


def _iam_fixture_metadata(argv, *, detail_rule):
    cmd = " ".join(argv)
    if "iam domain list" in cmd:
        return {"data": [{"display-name": "Default", "url": "https://identity.example.test"}]}
    if "dynamic-resource-groups list" in cmd:
        assert "--attributes matchingRule" in cmd
        return {"data": {"Resources": [{
            "id": "test-scim-group",
            "displayName": "katcha-recovery-candidates",
            "matchingRule": None,
        }]}}
    if "dynamic-resource-group get" in cmd:
        assert "--attributes matchingRule" in cmd
        assert "--dynamic-resource-group-id test-scim-group" in cmd
        return {"data": {"id": "test-scim-group", "matching-rule": detail_rule}}
    if "iam tag-namespace list" in cmd:
        return {"data": [{
            "name": "KatchaRecovery", "id": "ocid1.tagnamespace.test",
            "lifecycle-state": "ACTIVE"
        }]}
    if "iam tag list" in cmd:
        return {"data": [{"name": "Candidate", "lifecycle-state": "ACTIVE"}]}
    if "iam policy list" in cmd:
        return {"data": [{
            "name": "katcha-recovery-vault-access", "id": "ocid1.policy.test",
            "lifecycle-state": "ACTIVE"
        }]}
    if "iam policy get" in cmd:
        return {"data": {"statements": _grant()}}
    if "vault secret get" in cmd:
        return {"data": {
            "lifecycle-state": "ACTIVE",
            "compartment-id": "ocid1.compartment.test",
        }}
    raise AssertionError(f"Unexpected metadata request: {cmd}")


def test_oci_matching_rule_is_explicitly_fetched_when_list_field_null(monkeypatch):
    group_rule = (
        "All {instance.compartment.id = 'ocid1.compartment.test', "
        "tag.KatchaRecovery.Candidate.value = 'true'}"
    )
    calls = []

    def lookup(argv, **kwargs):
        calls.append(argv)
        return _iam_fixture_metadata(argv, detail_rule=group_rule)

    monkeypatch.setattr(audit, "metadata", lookup)
    findings = audit.iam_audit(["oci"], "ocid1.tenancy.test", _variables())
    assert findings
    assert all(passed for _name, passed in findings), findings
    assert any("dynamic-resource-group get" in " ".join(cmd) for cmd in calls)
    assert any(
        "dynamic-resource-groups list" in " ".join(cmd)
        and "--attributes matchingRule" in " ".join(cmd)
        for cmd in calls
    )


@pytest.mark.parametrize("rule", [None, "", 42, [], {"value": "unrecognized"}])
def test_oci_missing_dynamic_group_rule_is_unverified_not_crash(monkeypatch, rule):
    monkeypatch.setattr(
        audit,
        "metadata",
        lambda argv, **kwargs: _iam_fixture_metadata(argv, detail_rule=rule),
    )
    findings = dict(audit.iam_audit(
        ["oci"], "ocid1.tenancy.test", _variables()
    ))
    assert findings["default_domain_dynamic_group_matching_rule"] is False
    assert findings["active_defined_tag_namespace"] is True
    assert findings["candidate_four_secret_only_grant"] is True


@pytest.mark.parametrize(
    "payload, expected",
    [
        ({"matchingRule": None, "matching-rule": "abc"}, "abc"),
        ({"matchingRule": 3, "matching-rule": "abc"}, "abc"),
        ({"matchingRule": "  abc "}, "  abc "),
        ({"matching-rule": "x"}, "x"),
        ({"matchingRule": None}, ""),
        ({"matchingRule": {}}, ""),
        (None, ""),
    ],
)
def test_dynamic_group_rule_extraction_never_calls_regex_on_null(payload, expected):
    assert audit._dynamic_group_rule(payload) == expected
