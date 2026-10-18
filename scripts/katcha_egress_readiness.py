#!/usr/bin/env python3
"""Read-only, credential-free preflight for Katcha's proposed outbound worker.

Verifies public API connectivity and egress identity, not media access, anti-bot
eligibility, or permission to download any source.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import sys
from collections.abc import Callable
from urllib.request import Request, urlopen

IP_IDENTITY_URL = "https://api.ipify.org?format=json"
YOUTUBE_API_DISCOVERY_URL = "https://www.googleapis.com/discovery/v1/apis/youtube/v3/rest"


def public_egress_ip(*, timeout_seconds: float = 8.0) -> str:
    request = Request(IP_IDENTITY_URL, headers={"User-Agent": "KatchaEgressPreflight/1.0"})
    with urlopen(request, timeout=timeout_seconds) as response:
        payload = json.load(response)
    return str(payload["ip"])


def youtube_api_reachable(*, timeout_seconds: float = 8.0) -> bool:
    """Probe documented API discovery, never video playback or media endpoints."""
    request = Request(
        YOUTUBE_API_DISCOVERY_URL,
        headers={"User-Agent": "KatchaEgressPreflight/1.0"},
    )
    with urlopen(request, timeout=timeout_seconds) as response:
        return response.status == 200


def audit(
    *,
    primary_egress_ip: str | None = None,
    ip_reader: Callable[[], str] = public_egress_ip,
    api_probe: Callable[[], bool] = youtube_api_reachable,
) -> dict[str, object]:
    """Return separate connectivity and distinct-egress results without inferring success."""
    output: dict[str, object] = {
        "status": "BLOCKED",
        "egress_ip": None,
        "outbound_https": False,
        "api_discovery_reachable": False,
        "distinct_from_primary": None,
        "scope": "egress_identity_and_public_api_connectivity_only",
        "errors": [],
    }
    errors: list[str] = []
    primary = None
    if primary_egress_ip is not None:
        try:
            primary = ipaddress.IPv4Address(primary_egress_ip)
            if not primary.is_global:
                errors.append("primary_egress_address_not_public_ipv4")
                primary = None
        except ipaddress.AddressValueError:
            errors.append("invalid_primary_egress_ipv4")
    try:
        observed = ipaddress.IPv4Address(ip_reader())
        if not observed.is_global:
            errors.append("egress_address_not_public_ipv4")
        else:
            output["egress_ip"] = str(observed)
            output["outbound_https"] = True
            if primary is not None:
                output["distinct_from_primary"] = observed != primary
                if observed == primary:
                    errors.append("egress_ip_matches_primary")
    except (ValueError, TypeError, KeyError, OSError, TimeoutError) as exc:
        errors.append(f"egress_identity_failed:{type(exc).__name__}")

    try:
        output["api_discovery_reachable"] = bool(api_probe())
        if not output["api_discovery_reachable"]:
            errors.append("youtube_data_api_unreachable")
    except (ValueError, OSError, TimeoutError) as exc:
        errors.append(f"youtube_data_api_probe_failed:{type(exc).__name__}")

    output["errors"] = errors
    if not errors and output["egress_ip"] and output["api_discovery_reachable"]:
        output["status"] = (
            "DISTINCT_EGRESS_VERIFIED" if primary is not None else "CONNECTIVITY_ONLY"
        )
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--primary-egress-ip",
        help="PUBLIC IPv4 of Katcha primary, not its private VNIC address",
    )
    parser.add_argument("--timeout-seconds", type=float, default=8.0)
    args = parser.parse_args(argv)
    if not 0.5 <= args.timeout_seconds <= 30:
        parser.error("--timeout-seconds must be between 0.5 and 30")
    result = audit(
        primary_egress_ip=args.primary_egress_ip,
        ip_reader=lambda: public_egress_ip(timeout_seconds=args.timeout_seconds),
        api_probe=lambda: youtube_api_reachable(timeout_seconds=args.timeout_seconds),
    )
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] in {"DISTINCT_EGRESS_VERIFIED", "CONNECTIVITY_ONLY"} else 1


if __name__ == "__main__":
    sys.exit(main())
