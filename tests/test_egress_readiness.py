"""Offline tests for the dedicated outbound worker preflight."""

from __future__ import annotations

import importlib.util
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "katcha_egress_readiness.py"
spec = importlib.util.spec_from_file_location("katcha_egress_readiness", MODULE_PATH)
assert spec is not None and spec.loader is not None
readiness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(readiness)


def test_distinct_egress_requires_explicit_primary_identity():
    result = readiness.audit(
        primary_egress_ip="8.8.8.8",
        ip_reader=lambda: "1.1.1.1",
        api_probe=lambda: True,
    )
    assert result["status"] == "DISTINCT_EGRESS_VERIFIED"
    assert result["distinct_from_primary"] is True
    assert result["errors"] == []


def test_no_primary_never_claims_network_isolation():
    result = readiness.audit(ip_reader=lambda: "1.1.1.1", api_probe=lambda: True)
    assert result["status"] == "CONNECTIVITY_ONLY"
    assert result["distinct_from_primary"] is None


def test_same_public_ip_fails_closed():
    result = readiness.audit(
        primary_egress_ip="1.1.1.1",
        ip_reader=lambda: "1.1.1.1",
        api_probe=lambda: True,
    )
    assert result["status"] == "BLOCKED"
    assert "egress_ip_matches_primary" in result["errors"]


def test_invalid_primary_does_not_silently_lose_comparison():
    result = readiness.audit(
        primary_egress_ip="10.42.2.144/24",
        ip_reader=lambda: "1.1.1.1",
        api_probe=lambda: True,
    )
    assert result["status"] == "BLOCKED"
    assert "invalid_primary_egress_ipv4" in result["errors"]


def test_nonpublic_ip_rejected():
    result = readiness.audit(ip_reader=lambda: "10.42.2.144", api_probe=lambda: True)
    assert result["status"] == "BLOCKED"
    assert "egress_address_not_public_ipv4" in result["errors"]


def test_public_api_failure_reported_separately_from_egress():
    result = readiness.audit(ip_reader=lambda: "1.1.1.1", api_probe=lambda: False)
    assert result["outbound_https"] is True
    assert result["status"] == "BLOCKED"
    assert "youtube_data_api_unreachable" in result["errors"]


def test_connectivity_exception_is_redacted_to_class():
    def fail():
        raise OSError("sensitive network details")

    result = readiness.audit(ip_reader=fail, api_probe=lambda: True)
    assert result["status"] == "BLOCKED"
    assert result["errors"] == ["egress_identity_failed:OSError"]


def test_private_primary_rejected_even_if_distinct():
    result = readiness.audit(
        primary_egress_ip="10.42.2.144",
        ip_reader=lambda: "1.1.1.1",
        api_probe=lambda: True,
    )
    assert result["status"] == "BLOCKED"
    assert "primary_egress_address_not_public_ipv4" in result["errors"]
