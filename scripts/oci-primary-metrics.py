#!/usr/bin/env python3
"""Read-only OCI Compute Agent resource metrics for the verified Katcha A1 primary.

No shell pipelines, host changes, OCI mutations, credentials or raw provider errors.
Data is useful for monitoring, NOT an Oracle idle-reclamation guarantee.
"""
from __future__ import annotations

import json
import math
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

HOME = Path.home()
REPO = "Deveadra/katcha"
OCI = HOME / ".cache/katcha/oci-cli-3.94.1/bin/oci"
CONFIG = HOME / ".oci/config"
PROFILE = "KATCHA_VAULT_ADMIN"
REGION = "us-ashburn-1"
NAMESPACE = "oci_computeagent"
METRICS = (
    ("CpuUtilization", "mean", "%"),
    ("MemoryUtilization", "mean", "%"),
    ("NetworksBytesIn", "rate", "B/s"),
    ("NetworksBytesOut", "rate", "B/s"),
)
OCID = re.compile(r"ocid1\.(?:instance|compartment)\.[a-zA-Z0-9_.-]+")


class MetricsError(Exception):
    pass


def sanitized_command(argv: list[str], *, seconds: int = 40) -> str:
    """Capture without ever displaying OCI/gh output containing resource IDs or secrets."""
    try:
        proc = subprocess.run(argv, text=True, capture_output=True, check=False,
                              timeout=seconds)
    except subprocess.TimeoutExpired as exc:
        raise MetricsError("COMMAND_TIMEOUT") from exc
    except OSError as exc:
        raise MetricsError("COMMAND_UNAVAILABLE") from exc
    if proc.returncode:
        source = proc.stderr.lower()
        if any(s in source for s in (
            "notauthorizedornotfound", "authorizationfailed", "not authorized",
            "not authorized", "permission denied", "forbidden", "unauthorized",
        )):
            raise MetricsError("OCI_MONITORING_PERMISSION_DENIED")
        if "expired" in source or "invalidsecuritytoken" in source:
            raise MetricsError("OCI_ADMIN_SESSION_EXPIRED")
        raise MetricsError("COMMAND_FAILED_" + Path(argv[0]).name.upper().replace(".", "_"))
    return proc.stdout.strip()


def preflight() -> tuple[str, str]:
    if not OCI.is_file() or not CONFIG.is_file():
        raise MetricsError("OCI_CLI_OR_ADMIN_CONFIG_MISSING")
    try:
        sanitized_command([str(OCI), "--config-file", str(CONFIG), "--profile", PROFILE,
                           "--auth", "security_token", "session", "validate"], seconds=25)
    except MetricsError as exc:
        raise MetricsError(
            "ADMIN_SESSION_UNAVAILABLE: run bash scripts/katcha-oci-admin.sh reauth"
        ) from exc
    compartment = sanitized_command(
        ["gh", "variable", "get", "KATCHA_OCI_COMPARTMENT_ID", "-R", REPO], seconds=20
    )
    instance = sanitized_command(
        ["gh", "variable", "get", "KATCHA_OCI_PRIMARY_INSTANCE_ID", "-R", REPO], seconds=20
    )
    if not OCID.fullmatch(compartment) or not compartment.startswith("ocid1.compartment."):
        raise MetricsError("COMPARTMENT_ID_NOT_VERIFIED")
    if not OCID.fullmatch(instance) or not instance.startswith("ocid1.instance."):
        raise MetricsError("PRIMARY_INSTANCE_ID_NOT_VERIFIED")
    return compartment, instance


def metric_query(metric: str, stat: str, period: str, instance: str) -> str:
    assert metric in [item[0] for item in METRICS]
    assert stat in ("mean", "rate")
    assert period in ("5m", "1h")
    return f'{metric}[{period}]{{resourceId = "{instance}"}}.{stat}()'


def query_data(*, compartment: str, instance: str, metric: str, stat: str,
               period: str, start: datetime, end: datetime) -> list[tuple[datetime, float]]:
    arguments = [
        str(OCI), "--config-file", str(CONFIG), "--profile", PROFILE,
        "--auth", "security_token", "--region", REGION,
        "monitoring", "metric-data", "summarize-metrics-data",
        "--compartment-id", compartment,
        "--namespace", NAMESPACE,
        "--query-text", metric_query(metric, stat, period, instance),
        "--start-time", start.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "--end-time", end.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "--resolution", period,
        "--output", "json",
    ]
    try:
        record = json.loads(sanitized_command(arguments))
        streams = record["data"]
        if not isinstance(streams, list):
            raise ValueError("invalid response")
        # One primary resource and one metric stream; never accidentally combine
        # data from another instance/compartment or report a partial aggregate.
        if len(streams) != 1:
            raise MetricsError("NO_DATA" if not streams else "MULTIPLE_STREAMS_UNVERIFIED")
        dims = streams[0].get("dimensions", {})
        if not isinstance(dims, dict) or dims.get("resourceId") != instance:
            raise MetricsError("RESOURCE_ID_MISMATCH")
        points = streams[0]["aggregated-datapoints"]
        if not isinstance(points, list):
            raise ValueError("invalid datapoints")
        values = []
        for point in points:
            stamp = datetime.fromisoformat(point["timestamp"].replace("Z", "+00:00"))
            value = point["value"]
            if (stamp.tzinfo is None or isinstance(value, bool)
                    or not isinstance(value, (float, int))
                    or not math.isfinite(value) or value < 0):
                raise ValueError("invalid timestamp or value")
            if stat == "mean" and value > 100:
                raise ValueError("invalid utilization percentage")
            values.append((stamp.astimezone(UTC), float(value)))
        values.sort(key=lambda item: item[0])
        if len(set(x[0] for x in values)) != len(values):
            raise MetricsError("DUPLICATE_DATAPOINT")
        return values
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise MetricsError("INVALID_OR_NON_JSON_OCI_MONITORING_RESPONSE") from exc


def format_value(value: float, unit: str) -> str:
    if unit == "%":
        return f"{value:.1f}%"
    return f"{value:,.0f} B/s"


def display(*, now: datetime, near: dict[str, list[tuple[datetime, float]]],
            week: dict[str, list[tuple[datetime, float]]]) -> int:
    print("OCI A1 primary monitoring (read-only; Oracle oci_computeagent)")
    print("as_of_utc:", now.strftime("%Y-%m-%dT%H:%M:%SZ"))
    complete = True
    for metric, _stat, unit in METRICS:
        recent = near[metric]
        historical = week[metric]
        if not recent or not historical:
            print(metric + ": NO_DATA")
            complete = False
            continue
        latest_time, latest = recent[-1]
        age = (now - latest_time).total_seconds() / 60
        mean6h = sum(val for _, val in recent) / len(recent)
        mean7d = sum(val for _, val in historical) / len(historical)
        first = historical[0][0].strftime("%Y-%m-%dT%H:%MZ")
        last = historical[-1][0].strftime("%Y-%m-%dT%H:%MZ")
        print(metric + ": latest=" + format_value(latest, unit)
              + " six_hour_mean=" + format_value(mean6h, unit)
              + " seven_day_hourly_mean=" + format_value(mean7d, unit)
              + " last_age_min=" + f"{age:.0f}"
              + " seven_day_samples=" + str(len(historical)))
        print("  hourly_coverage_utc:", first, "to", last)
        if age < -5 or age > 20:
            print("  WARNING: STALE_OR_FUTURE_DATAPOINT")
            complete = False
    print("NOTE: hourly means are not Oracle's seven-day CPU 95th percentile or"
          " network utilization percentage.")
    print("NOTE: memory above 20% now is not a seven-day retention guarantee.")
    print("OCI_MONITORING_REPORT:", "COMPLETE" if complete else "INCOMPLETE")
    return 0 if complete else 2


def main() -> int:
    try:
        compartment, instance = preflight()
        now = datetime.now(UTC)
        near = {}
        week = {}
        for metric, statistic, _ in METRICS:
            try:
                near[metric] = query_data(
                    compartment=compartment, instance=instance, metric=metric, stat=statistic,
                    period="5m", start=now - timedelta(hours=6), end=now
                )
                week[metric] = query_data(
                    compartment=compartment, instance=instance, metric=metric, stat=statistic,
                    period="1h", start=now - timedelta(days=7), end=now
                )
            except MetricsError as exc:
                raise MetricsError(metric + ": " + str(exc)) from exc
        return display(now=now, near=near, week=week)
    except MetricsError as exc:
        print("OCI_METRICS_STOP: " + str(exc), file=sys.stderr)
        print("No resource settings were modified. If authentication fails, run"
              " 'bash scripts/katcha-oci-admin.sh reauth' and retry.", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("OCI_METRICS_STOP: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
