"""Offline negative and positive contracts for the guest-side OCI Vault probe."""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
from pathlib import Path

import pytest

SOURCE = Path("scripts/oci-candidate-vault-principal-probe.py")
SPEC = importlib.util.spec_from_file_location("oci_vault_candidate_probe", SOURCE)
assert SPEC and SPEC.loader
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)

IDS = tuple(f"ocid1.vaultsecret.oc1.test.bundle{i}" for i in range(4))
FIFTH = "ocid1.vaultsecret.oc1.test.knownfifth"


def test_identity_is_bound_to_actual_instance_metadata():
    def fetch(request, timeout):
        assert request.full_url == probe.IMDS_URL
        assert request.get_header("Authorization") == "Bearer Oracle"
        assert timeout == 5
        return io.BytesIO(b'{"id":"ocid1.instance.oc1.test.candidate"}')

    assert probe.identity(fetch) == "ocid1.instance.oc1.test.candidate"


@pytest.mark.parametrize("document", [b'{}', b'{"id":"other"}', b'[]'])
def test_identity_rejects_untrusted_metadata(document):
    with pytest.raises(probe.AcceptanceError, match="invalid OCI instance metadata"):
        probe.identity(lambda _request, timeout: io.BytesIO(document))


def test_secret_ids_require_four_distinct_valid_values():
    good = dict(zip(probe.BUNDLES, IDS, strict=True))
    assert probe.verify_ids(good) == IDS
    with pytest.raises(probe.AcceptanceError):
        probe.verify_ids({**good, probe.BUNDLES[-1]: IDS[0]})
    with pytest.raises(probe.AcceptanceError):
        probe.verify_ids({**good, probe.BUNDLES[-1]: "not-a-secret"})


def test_untagged_must_deny_every_authorized_secret():
    seen = []
    def deny(argv):
        seen.append(argv[4])
        return False, ""
    result = probe.evaluate("deny-four", IDS, forbidden_id=None, run=deny)
    assert result["status"] == "UNTAGGED_FOUR_DENIED"
    assert result["full_recovery_verified"] is False
    assert seen == list(IDS)


def test_untagged_unexpected_success_is_a_hard_failure():
    with pytest.raises(probe.AcceptanceError, match="unexpectedly accessed"):
        probe.evaluate("deny-four", IDS, forbidden_id=None, run=lambda argv: (True, argv[4]))


def test_tagged_requires_four_allowed_and_known_fifth_denied():
    seen = []
    def check(argv):
        identifier = argv[4]
        seen.append(identifier)
        return (False, "") if identifier == FIFTH else (True, identifier)
    result = probe.evaluate("allow-four", IDS, forbidden_id=FIFTH, run=check)
    assert result["status"] == "TAGGED_FOUR_ALLOWED_FIFTH_DENIED"
    assert result["secret_access_checks"] == 5
    assert seen == [*IDS, FIFTH]


def test_tagged_wrong_bundle_identity_is_not_a_pass():
    with pytest.raises(probe.AcceptanceError, match="could not verify"):
        probe.evaluate("allow-four", IDS, forbidden_id=FIFTH, run=lambda argv: (True, "wrong"))


def test_tagged_unexpected_fifth_access_is_a_hard_failure():
    with pytest.raises(probe.AcceptanceError, match="unexpectedly accessed forbidden"):
        probe.evaluate("allow-four", IDS, forbidden_id=FIFTH, run=lambda argv: (True, argv[4]))


@pytest.mark.parametrize("fifth", [None, IDS[0], "ocid1.instance.invalid"])
def test_tagged_refuses_unverified_forbidden_fixture(fifth):
    with pytest.raises(probe.AcceptanceError):
        probe.evaluate("allow-four", IDS, forbidden_id=fifth)


def test_cli_uses_instance_principal_and_returns_only_id():
    seen = []
    def runner(argv, **kwargs):
        seen.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout=IDS[0] + "\n", stderr="")
    ok, value = probe.command(["secrets", "secret-bundle", "get"], run=runner)
    assert ok and value == IDS[0]
    assert seen[0][0][:3] == ["oci", "--auth", "instance_principal"]
    assert seen[0][1]["capture_output"] is True
    assert seen[0][1]["timeout"] == 60


def test_denial_must_be_authorization_failure_not_network_outage():
    def denied(argv, **_kwargs):
        return subprocess.CompletedProcess(
            argv, 1, stdout="", stderr="ServiceError: NotAuthorizedOrNotFound"
        )
    assert probe.command(["secrets", "secret-bundle", "get"], run=denied) == (False, "")

    def interrupted(argv, **_kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="connection timed out")
    with pytest.raises(probe.AcceptanceError, match="without an authorization-denial"):
        probe.command(["secrets", "secret-bundle", "get"], run=interrupted)


def test_cli_does_not_echo_provider_secret_body_on_error():
    def failed(argv, **_kwargs):
        return subprocess.CompletedProcess(
            argv, 1, stdout="SENSITIVE_BUNDLE_CONTENT",
            stderr="network error SENSITIVE_BUNDLE_CONTENT"
        )
    with pytest.raises(probe.AcceptanceError) as exc:
        probe.command(["secrets", "secret-bundle", "get"], run=failed)
    assert "SENSITIVE_BUNDLE_CONTENT" not in str(exc.value)


def test_main_fails_before_metadata_or_vault_without_explicit_approval(monkeypatch, capsys):
    monkeypatch.setattr(probe, "identity", lambda: pytest.fail("must not touch metadata"))
    status = probe.main(["deny-four", "--expected-instance-id", "ocid1.instance.test"])
    assert status == 2
    assert json.loads(capsys.readouterr().out)["status"] == "BLOCKED"


def test_main_refuses_different_instance_before_vault(monkeypatch):
    monkeypatch.setattr(probe, "identity", lambda: "ocid1.instance.actual")
    monkeypatch.setattr(probe, "verify_ids", lambda source: pytest.fail("must not probe Vault"))
    assert probe.main([
        "deny-four", "--expected-instance-id", "ocid1.instance.expected",
        "--approve-live-vault-reads"
    ]) == 2
