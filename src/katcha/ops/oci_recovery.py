from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx


class RecoveryError(RuntimeError):
    pass


class CapacityUnavailable(RecoveryError):
    pass


class CommitOutcomeUnknown(RecoveryError):
    """Authority commit may have succeeded; destructive rollback is unsafe."""


def _env(name: str, *, default: str | None = None) -> str:
    value = os.environ.get(name)
    if value is None or not str(value).strip():
        if default is not None and str(default).strip():
            return str(default).strip()
        raise RecoveryError(f"missing required recovery setting: {name}")
    return str(value).strip()


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().casefold() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Incident:
    incident_id: str
    expected_active_epoch: int
    active_deployment_id: str

    @classmethod
    def from_event(cls, path: Path) -> Incident:
        payload = json.loads(path.read_text(encoding="utf-8"))
        body = payload.get("client_payload", payload)
        active = body.get("active_deployment") or {}
        return cls(
            incident_id=str(body["incident_id"]),
            expected_active_epoch=int(body["expected_active_epoch"]),
            active_deployment_id=str(active.get("deployment_id") or ""),
        )


@dataclass(frozen=True, slots=True)
class ShapePlan:
    mode: str
    shape: str
    image_id: str
    ocpus: float
    memory_gb: float
    paid: bool


@dataclass(frozen=True, slots=True)
class RecoveryTarget:
    availability_domain: str
    subnet_id: str


@dataclass(frozen=True, slots=True)
class RecoveryConfig:
    coordinator_url: str
    coordinator_admin_token: str
    candidate_token: str
    public_health_url: str
    release_sha: str
    availability_domain: str
    compartment_id: str
    subnet_id: str
    alternate_targets: tuple[RecoveryTarget, ...]
    data_volume_id: str
    data_volume_fs_uuid: str
    production_env_secret_id: str
    aws_bundle_secret_id: str
    backup_env_secret_id: str
    restore_env_secret_id: str
    assign_public_ip: bool
    primary: ShapePlan
    fallback: ShapePlan
    paid_enabled: bool
    paid_ttl_hours: int
    paid_estimated_hourly_usd: float
    paid_max_incident_usd: float
    paid_max_concurrent: int
    candidate_timeout_seconds: int
    cross_ad_volume_size_gb: int
    cross_ad_device_path: str
    cross_ad_max_backup_age_seconds: int
    bootstrap_template: Path

    @classmethod
    def from_env(
        cls,
        *,
        require_external_compute: bool = True,
    ) -> RecoveryConfig:
        paid_enabled = _bool_env("KATCHA_OCI_PAID_FALLBACK_ENABLED", False)
        paid_ttl = int(_env("KATCHA_OCI_PAID_FALLBACK_TTL_HOURS", default="24"))
        paid_hourly = float(
            _env("KATCHA_OCI_PAID_FALLBACK_ESTIMATED_HOURLY_USD", default="0")
        )
        paid_max = float(
            _env("KATCHA_OCI_PAID_FALLBACK_MAX_INCIDENT_USD", default="0")
        )
        paid_concurrent = int(
            _env("KATCHA_OCI_PAID_FALLBACK_MAX_CONCURRENT", default="1")
        )
        primary_image = _env("KATCHA_OCI_PRIMARY_IMAGE_ID")
        try:
            raw_targets = json.loads(
                _env("KATCHA_OCI_CROSS_AD_TARGETS_JSON", default="[]")
            )
        except json.JSONDecodeError as exc:
            raise RecoveryError(
                "KATCHA_OCI_CROSS_AD_TARGETS_JSON must be valid JSON"
            ) from exc
        if not isinstance(raw_targets, list):
            raise RecoveryError(
                "KATCHA_OCI_CROSS_AD_TARGETS_JSON must be a JSON array"
            )
        alternate_targets: list[RecoveryTarget] = []
        for row in raw_targets:
            if not isinstance(row, dict):
                raise RecoveryError("each cross-AD target must be a JSON object")
            availability_domain = str(row.get("availability_domain") or "").strip()
            subnet_id = str(row.get("subnet_id") or "").strip()
            if not availability_domain or not subnet_id:
                raise RecoveryError(
                    "each cross-AD target requires availability_domain and subnet_id"
                )
            alternate_targets.append(
                RecoveryTarget(
                    availability_domain=availability_domain,
                    subnet_id=subnet_id,
                )
            )
        fallback_shape = (
            _env("KATCHA_OCI_PAID_FALLBACK_SHAPE")
            if paid_enabled
            else "disabled"
        )
        fallback_image = (
            _env("KATCHA_OCI_PAID_FALLBACK_IMAGE_ID")
            if paid_enabled
            else primary_image
        )
        config = cls(
            coordinator_url=_env("KATCHA_RECOVERY_COORDINATOR_URL").rstrip("/"),
            coordinator_admin_token=_env("KATCHA_RECOVERY_ADMIN_TOKEN"),
            candidate_token=_env("KATCHA_RECOVERY_CANDIDATE_TOKEN"),
            public_health_url=_env("KATCHA_PUBLIC_HEALTH_URL"),
            release_sha=_env("KATCHA_RELEASE_SHA"),
            availability_domain=_env("KATCHA_OCI_AVAILABILITY_DOMAIN"),
            compartment_id=_env("KATCHA_OCI_COMPARTMENT_ID"),
            subnet_id=_env("KATCHA_OCI_SUBNET_ID"),
            alternate_targets=tuple(alternate_targets),
            data_volume_id=_env("KATCHA_OCI_DATA_VOLUME_ID"),
            data_volume_fs_uuid=_env("KATCHA_OCI_DATA_VOLUME_FS_UUID"),
            production_env_secret_id=_env("KATCHA_OCI_PRODUCTION_ENV_SECRET_ID"),
            aws_bundle_secret_id=_env("KATCHA_OCI_AWS_BUNDLE_SECRET_ID"),
            backup_env_secret_id=_env("KATCHA_OCI_BACKUP_ENV_SECRET_ID"),
            restore_env_secret_id=_env("KATCHA_OCI_RESTORE_ENV_SECRET_ID"),
            assign_public_ip=_bool_env("KATCHA_OCI_ASSIGN_PUBLIC_IP", True),
            primary=ShapePlan(
                mode="always-free-a1",
                shape=_env("KATCHA_OCI_PRIMARY_SHAPE", default="VM.Standard.A1.Flex"),
                image_id=primary_image,
                ocpus=float(_env("KATCHA_OCI_PRIMARY_OCPUS", default="2")),
                memory_gb=float(_env("KATCHA_OCI_PRIMARY_MEMORY_GB", default="12")),
                paid=False,
            ),
            fallback=ShapePlan(
                mode="paid-fallback",
                shape=fallback_shape,
                image_id=fallback_image,
                ocpus=float(_env("KATCHA_OCI_PAID_FALLBACK_OCPUS", default="1")),
                memory_gb=float(
                    _env("KATCHA_OCI_PAID_FALLBACK_MEMORY_GB", default="8")
                ),
                paid=True,
            ),
            paid_enabled=paid_enabled,
            paid_ttl_hours=paid_ttl,
            paid_estimated_hourly_usd=paid_hourly,
            paid_max_incident_usd=paid_max,
            paid_max_concurrent=paid_concurrent,
            candidate_timeout_seconds=int(
                _env("KATCHA_RECOVERY_CANDIDATE_TIMEOUT_SECONDS", default="1200")
            ),
            cross_ad_volume_size_gb=int(
                _env("KATCHA_OCI_CROSS_AD_DATA_VOLUME_SIZE_GB", default="50")
            ),
            cross_ad_device_path=_env(
                "KATCHA_OCI_DATA_VOLUME_DEVICE_PATH",
                default="/dev/oracleoci/oraclevdb",
            ),
            cross_ad_max_backup_age_seconds=int(
                _env("KATCHA_OCI_CROSS_AD_MAX_BACKUP_AGE_SECONDS", default="7200")
            ),
            bootstrap_template=Path(
                _env(
                    "KATCHA_OCI_RECOVERY_BOOTSTRAP_TEMPLATE",
                    default="deploy/cloud-init/oci-recovery-candidate.sh.tmpl",
                )
            ),
        )
        config.validate(require_external_compute=require_external_compute)
        return config

    def validate(self, *, require_external_compute: bool = True) -> None:
        if (
            require_external_compute
            and not _bool_env("KATCHA_EXTERNAL_COMPUTE_ENABLED", False)
        ):
            raise RecoveryError("external compute kill switch is disabled")
        if self.paid_ttl_hours < 1 or self.paid_ttl_hours > 72:
            raise RecoveryError("paid fallback TTL must be between 1 and 72 hours")
        if self.paid_max_concurrent != 1:
            raise RecoveryError("paid fallback concurrency must be exactly 1")
        if self.paid_enabled:
            if self.paid_estimated_hourly_usd <= 0:
                raise RecoveryError("paid fallback hourly estimate must be positive")
            if self.paid_max_incident_usd <= 0:
                raise RecoveryError("paid fallback incident budget must be positive")
            worst_case = self.paid_estimated_hourly_usd * self.paid_ttl_hours
            if worst_case > self.paid_max_incident_usd + 1e-9:
                raise RecoveryError(
                    "paid fallback TTL exceeds the configured incident budget"
                )
        if len(self.release_sha) != 40:
            raise RecoveryError("KATCHA_RELEASE_SHA must be an exact git SHA")
        if self.cross_ad_volume_size_gb < 50:
            raise RecoveryError("cross-AD data volume must be at least 50 GB")
        if self.cross_ad_max_backup_age_seconds < 900:
            raise RecoveryError("cross-AD backup freshness window must be at least 900 seconds")
        if not self.cross_ad_device_path.startswith("/dev/oracleoci/"):
            raise RecoveryError("cross-AD recovery requires a consistent OCI device path")
        seen_targets = {(self.availability_domain, self.subnet_id)}
        for target in self.alternate_targets:
            key = (target.availability_domain, target.subnet_id)
            if key in seen_targets:
                raise RecoveryError("cross-AD recovery targets must be unique")
            seen_targets.add(key)


class CoordinatorClient:
    def __init__(self, config: RecoveryConfig) -> None:
        self.base = config.coordinator_url
        self.admin_token = config.coordinator_admin_token

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        response = httpx.request(
            method,
            f"{self.base}{path}",
            headers={"Authorization": f"Bearer {self.admin_token}"},
            json=payload,
            timeout=15.0,
        )
        if response.status_code >= 400:
            raise RecoveryError(
                f"coordinator {path} returned HTTP {response.status_code}: "
                f"{response.text[:300]}"
            )
        data = response.json()
        if not isinstance(data, dict):
            raise RecoveryError(f"coordinator {path} returned invalid JSON")
        return data

    def status(self) -> dict[str, Any]:
        return self._request("GET", "/v1/authority/status")

    def prepare(
        self,
        *,
        deployment_id: str,
        health_url: str,
        expected_active_epoch: int,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/authority/prepare",
            payload={
                "deployment_id": deployment_id,
                "health_url": health_url,
                "expected_active_epoch": expected_active_epoch,
            },
        )

    def commit(
        self,
        *,
        deployment_id: str,
        deployment_epoch: int,
        expected_active_epoch: int,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/authority/commit",
            payload={
                "deployment_id": deployment_id,
                "deployment_epoch": deployment_epoch,
                "expected_active_epoch": expected_active_epoch,
            },
        )

    def abort(self, *, deployment_id: str, deployment_epoch: int) -> None:
        self._request(
            "POST",
            "/v1/authority/abort",
            payload={
                "deployment_id": deployment_id,
                "deployment_epoch": deployment_epoch,
            },
        )


class OciCli:
    def run(self, args: list[str]) -> dict[str, Any]:
        command = ["oci", *args, "--output", "json"]
        try:
            completed = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            message = "\n".join(filter(None, [exc.stdout, exc.stderr]))
            lowered = message.casefold()
            if (
                "outofhostcapacity" in lowered
                or "out of host capacity" in lowered
                or "insufficient host capacity" in lowered
            ):
                raise CapacityUnavailable(message[:1000]) from exc
            raise RecoveryError(
                f"OCI command failed ({' '.join(command[:4])}): {message[:1000]}"
            ) from exc
        if not completed.stdout.strip():
            return {}
        data = json.loads(completed.stdout)
        return data if isinstance(data, dict) else {"data": data}

    def list_instances(self, config: RecoveryConfig) -> list[dict[str, Any]]:
        data = self.run(
            [
                "compute",
                "instance",
                "list",
                "--compartment-id",
                config.compartment_id,
                "--all",
            ]
        ).get("data", [])
        return list(data) if isinstance(data, list) else []

    def get_instance(self, instance_id: str) -> dict[str, Any]:
        data = self.run(
            ["compute", "instance", "get", "--instance-id", instance_id]
        ).get("data", {})
        return dict(data) if isinstance(data, dict) else {}

    def launch(
        self,
        config: RecoveryConfig,
        incident: Incident,
        *,
        deployment_id: str,
        deployment_epoch: int,
        plan: ShapePlan,
        target: RecoveryTarget,
        recovery_mode: str,
        data_volume_id: str,
        storage_mode: str,
        user_data_path: Path,
    ) -> str:
        expires = ""
        if plan.paid:
            expires = (
                datetime.now(UTC) + timedelta(hours=config.paid_ttl_hours)
            ).isoformat()
        tags = {
            "KatchaRole": "control-plane",
            "KatchaRecoveryIncident": incident.incident_id,
            "KatchaDeploymentId": deployment_id,
            "KatchaDeploymentEpoch": str(deployment_epoch),
            "KatchaRecoveryMode": recovery_mode,
            "KatchaPaidFallback": str(plan.paid).lower(),
            "KatchaStorageMode": storage_mode,
            "KatchaDataVolumeId": data_volume_id,
            "KatchaSubnetId": target.subnet_id,
        }
        if expires:
            tags["KatchaExpiresAt"] = expires
        result = self.run(
            [
                "compute",
                "instance",
                "launch",
                "--availability-domain",
                target.availability_domain,
                "--compartment-id",
                config.compartment_id,
                "--subnet-id",
                target.subnet_id,
                "--image-id",
                plan.image_id,
                "--shape",
                plan.shape,
                "--shape-config",
                json.dumps(
                    {"ocpus": plan.ocpus, "memoryInGBs": plan.memory_gb},
                    separators=(",", ":"),
                ),
                "--assign-public-ip",
                str(config.assign_public_ip).lower(),
                "--display-name",
                f"katcha-recovery-{incident.incident_id[:8]}-{recovery_mode[:12]}",
                "--freeform-tags",
                json.dumps(tags, separators=(",", ":")),
                "--user-data-file",
                str(user_data_path),
                "--wait-for-state",
                "RUNNING",
                "--max-wait-seconds",
                "1200",
            ]
        )
        instance_id = str((result.get("data") or {}).get("id") or "")
        if not instance_id:
            raise RecoveryError("OCI launch returned no instance OCID")
        return instance_id

    def volume_attachments(
        self,
        config: RecoveryConfig,
        volume_id: str,
    ) -> list[dict[str, Any]]:
        data = self.run(
            [
                "compute",
                "volume-attachment",
                "list",
                "--compartment-id",
                config.compartment_id,
                "--volume-id",
                volume_id,
                "--all",
            ]
        ).get("data", [])
        return list(data) if isinstance(data, list) else []

    def stop(self, instance_id: str) -> None:
        instance = self.get_instance(instance_id)
        state = str(
            instance.get("lifecycle-state")
            or instance.get("lifecycleState")
            or ""
        )
        if state in {"STOPPED", "TERMINATED", "TERMINATING"}:
            return
        self.run(
            [
                "compute",
                "instance",
                "action",
                "--instance-id",
                instance_id,
                "--action",
                "STOP",
                "--wait-for-state",
                "STOPPED",
                "--max-wait-seconds",
                "1200",
            ]
        )

    def start(self, instance_id: str) -> None:
        self.run(
            [
                "compute",
                "instance",
                "action",
                "--instance-id",
                instance_id,
                "--action",
                "START",
                "--wait-for-state",
                "RUNNING",
                "--max-wait-seconds",
                "1200",
            ]
        )

    def detach(self, attachment_id: str) -> None:
        self.run(
            [
                "compute",
                "volume-attachment",
                "detach",
                "--volume-attachment-id",
                attachment_id,
                "--force",
                "--wait-for-state",
                "SUCCEEDED",
                "--max-wait-seconds",
                "1200",
            ]
        )

    def attach(
        self,
        instance_id: str,
        volume_id: str,
        *,
        device_path: str,
    ) -> None:
        self.run(
            [
                "compute",
                "volume-attachment",
                "attach",
                "--instance-id",
                instance_id,
                "--volume-id",
                volume_id,
                "--type",
                "paravirtualized",
                "--device",
                device_path,
                "--wait-for-state",
                "ATTACHED",
                "--max-wait-seconds",
                "1200",
            ]
        )

    def create_recovery_volume(
        self,
        config: RecoveryConfig,
        incident: Incident,
        *,
        deployment_id: str,
        availability_domain: str,
    ) -> str:
        tags = {
            "KatchaRole": "recovery-data",
            "KatchaRecoveryIncident": incident.incident_id,
            "KatchaDeploymentId": deployment_id,
            "KatchaEphemeralRecoveryVolume": "true",
        }
        result = self.run(
            [
                "bv",
                "volume",
                "create",
                "--availability-domain",
                availability_domain,
                "--compartment-id",
                config.compartment_id,
                "--size-in-gbs",
                str(config.cross_ad_volume_size_gb),
                "--display-name",
                f"katcha-recovery-data-{incident.incident_id[:8]}",
                "--freeform-tags",
                json.dumps(tags, separators=(",", ":")),
                "--wait-for-state",
                "AVAILABLE",
                "--max-wait-seconds",
                "1200",
            ]
        )
        volume_id = str((result.get("data") or {}).get("id") or "")
        if not volume_id:
            raise RecoveryError("OCI volume create returned no volume OCID")
        return volume_id

    def delete_volume(self, volume_id: str) -> None:
        self.run(
            [
                "bv",
                "volume",
                "delete",
                "--volume-id",
                volume_id,
                "--force",
            ]
        )

    def softstop(self, instance_id: str) -> None:
        instance = self.get_instance(instance_id)
        state = str(
            instance.get("lifecycle-state")
            or instance.get("lifecycleState")
            or ""
        )
        if state in {"STOPPED", "TERMINATED", "TERMINATING"}:
            return
        self.run(
            [
                "compute",
                "instance",
                "action",
                "--instance-id",
                instance_id,
                "--action",
                "SOFTSTOP",
                "--wait-for-state",
                "STOPPED",
                "--max-wait-seconds",
                "1200",
            ]
        )

    def terminate(self, instance_id: str) -> None:
        self.run(
            [
                "compute",
                "instance",
                "terminate",
                "--instance-id",
                instance_id,
                "--preserve-boot-volume",
                "false",
                "--force",
                "--wait-for-state",
                "SUCCEEDED",
                "--max-wait-seconds",
                "1200",
            ]
        )


def _tags(row: dict[str, Any]) -> dict[str, str]:
    value = row.get("freeform-tags") or row.get("freeformTags") or {}
    return {str(k): str(v) for k, v in value.items()} if isinstance(value, dict) else {}


def _state(row: dict[str, Any]) -> str:
    return str(row.get("lifecycle-state") or row.get("lifecycleState") or "")


def _instance_id(row: dict[str, Any]) -> str:
    return str(row.get("id") or "")


def find_deployment_instance(
    rows: list[dict[str, Any]],
    deployment_id: str,
) -> dict[str, Any] | None:
    if not deployment_id:
        return None
    for row in rows:
        if _state(row) in {"TERMINATED", "TERMINATING"}:
            continue
        if _tags(row).get("KatchaDeploymentId") == deployment_id:
            return row
    return None


def find_existing_candidate(
    rows: list[dict[str, Any]],
    incident_id: str,
) -> dict[str, Any] | None:
    for row in rows:
        if _state(row) in {"TERMINATED", "TERMINATING"}:
            continue
        if _tags(row).get("KatchaRecoveryIncident") == incident_id:
            return row
    return None


def paid_instance_count(rows: list[dict[str, Any]]) -> int:
    return sum(
        1
        for row in rows
        if _state(row) not in {"TERMINATED", "TERMINATING"}
        and _tags(row).get("KatchaRecoveryMode") == "paid-fallback"
    )


def render_bootstrap(
    config: RecoveryConfig,
    *,
    deployment_id: str,
    deployment_epoch: int,
) -> Path:
    content = config.bootstrap_template.read_text(encoding="utf-8")
    replacements = {
        "DEPLOYMENT_ID": deployment_id,
        "DEPLOYMENT_EPOCH": str(deployment_epoch),
        "RELEASE_SHA": config.release_sha,
        "DATA_VOLUME_FS_UUID": config.data_volume_fs_uuid,
        "PRODUCTION_ENV_SECRET_ID": config.production_env_secret_id,
        "AWS_BUNDLE_SECRET_ID": config.aws_bundle_secret_id,
        "BACKUP_ENV_SECRET_ID": config.backup_env_secret_id,
        "RESTORE_ENV_SECRET_ID": config.restore_env_secret_id,
        "RECOVERY_COORDINATOR_URL": config.coordinator_url,
        "RECOVERY_CANDIDATE_TOKEN": config.candidate_token,
        "PUBLIC_HEALTH_URL": config.public_health_url,
    }
    for name, value in replacements.items():
        placeholder = f"__{name}__"
        if placeholder not in content:
            raise RecoveryError(f"bootstrap template is missing {placeholder}")
        content = content.replace(placeholder, shlex.quote(value))
    if "__" in content:
        unresolved = sorted(
            {
                token.split("__", 1)[0]
                for token in content.split("__")[1::2]
                if token
            }
        )
        if unresolved:
            raise RecoveryError("bootstrap template has unresolved placeholders")
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix="katcha-recovery-",
        suffix=".sh",
        delete=False,
    ) as handle:
        handle.write(content)
        handle.flush()
        path = Path(handle.name)
    path.chmod(0o600)
    return path


def validate_incident(state: dict[str, Any], incident: Incident) -> None:
    active = state.get("active") or {}
    current_incident = state.get("incident") or {}
    if int(active.get("epoch") or 0) != incident.expected_active_epoch:
        raise RecoveryError("incident active epoch is stale")
    if incident.active_deployment_id and (
        str(active.get("deployment_id") or "") != incident.active_deployment_id
    ):
        raise RecoveryError("incident active deployment is stale")
    if str(current_incident.get("id") or "") != incident.incident_id:
        raise RecoveryError("coordinator no longer recognizes this recovery incident")


def wait_for_candidate_ready(
    coordinator: CoordinatorClient,
    *,
    deployment_id: str,
    deployment_epoch: int,
    timeout_seconds: int,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        state = coordinator.status()
        pending = state.get("pending") or {}
        if (
            pending.get("deployment_id") == deployment_id
            and int(pending.get("epoch") or 0) == deployment_epoch
            and pending.get("ready_at")
        ):
            return
        time.sleep(10)
    raise RecoveryError("candidate did not pass readiness before timeout")


def commit_authority_safely(
    coordinator: CoordinatorClient,
    *,
    deployment_id: str,
    deployment_epoch: int,
    expected_active_epoch: int,
) -> dict[str, Any]:
    try:
        return coordinator.commit(
            deployment_id=deployment_id,
            deployment_epoch=deployment_epoch,
            expected_active_epoch=expected_active_epoch,
        )
    except Exception as commit_error:
        try:
            state = coordinator.status()
        except Exception as status_error:
            raise CommitOutcomeUnknown(
                "authority commit outcome is unknown; candidate remains in place "
                "and must not be rolled back automatically"
            ) from status_error

        active = state.get("active") or {}
        if (
            str(active.get("deployment_id") or "") == deployment_id
            and int(active.get("epoch") or 0) == deployment_epoch
        ):
            return {
                "active": active,
                "active_epoch": deployment_epoch,
                "leader_id": deployment_id,
                "reconciled_after_commit_error": True,
            }

        pending = state.get("pending") or {}
        active_epoch = int(active.get("epoch") or 0)
        if (
            active_epoch == expected_active_epoch
            and str(pending.get("deployment_id") or "") == deployment_id
            and int(pending.get("epoch") or 0) == deployment_epoch
        ):
            raise RecoveryError(
                "authority commit failed and coordinator confirms the candidate "
                "is still pending"
            ) from commit_error

        raise CommitOutcomeUnknown(
            "authority changed unexpectedly while commit result was uncertain; "
            "automatic rollback is unsafe"
        ) from commit_error


def _attachment_instance_id(row: dict[str, Any]) -> str:
    return str(row.get("instance-id") or row.get("instanceId") or "")


def _attachment_id(row: dict[str, Any]) -> str:
    return str(row.get("id") or "")


def switch_volume(
    oci: OciCli,
    config: RecoveryConfig,
    candidate_id: str,
) -> str | None:
    previous_instance: str | None = None
    attachments = oci.volume_attachments(config)
    for attachment in attachments:
        state = _state(attachment)
        attached_instance = _attachment_instance_id(attachment)
        if state not in {"ATTACHED", "ATTACHING"} or not attached_instance:
            continue
        if attached_instance == candidate_id:
            return None
        previous_instance = attached_instance
        oci.stop(attached_instance)
        oci.detach(_attachment_id(attachment))
    oci.attach(config, candidate_id)
    return previous_instance


def rollback_volume(
    oci: OciCli,
    config: RecoveryConfig,
    *,
    candidate_id: str,
    previous_instance_id: str | None,
) -> None:
    for attachment in oci.volume_attachments(config):
        if _attachment_instance_id(attachment) == candidate_id:
            oci.detach(_attachment_id(attachment))
    if previous_instance_id:
        oci.attach(config, previous_instance_id)
        oci.start(previous_instance_id)


def recover(event_path: Path, config: RecoveryConfig, oci: OciCli) -> dict[str, Any]:
    incident = Incident.from_event(event_path)
    coordinator = CoordinatorClient(config)
    state = coordinator.status()
    validate_incident(state, incident)

    deployment_id = f"oci-recovery-{incident.incident_id[:12]}"
    prepared = coordinator.prepare(
        deployment_id=deployment_id,
        health_url=config.public_health_url,
        expected_active_epoch=incident.expected_active_epoch,
    )
    pending = prepared.get("pending") or {}
    deployment_epoch = int(pending["epoch"])

    rows = oci.list_instances(config)
    existing = find_existing_candidate(rows, incident.incident_id)
    candidate_id = _instance_id(existing) if existing else ""
    mode = _tags(existing).get("KatchaRecoveryMode", "") if existing else ""
    user_data_path: Path | None = None
    previous_instance_id: str | None = None
    active_instance = find_deployment_instance(
        rows,
        incident.active_deployment_id,
    )
    rollback_instance_id = _instance_id(active_instance) if active_instance else None

    try:
        if not candidate_id:
            user_data_path = render_bootstrap(
                config,
                deployment_id=deployment_id,
                deployment_epoch=deployment_epoch,
            )
            try:
                candidate_id = oci.launch(
                    config,
                    incident,
                    deployment_id=deployment_id,
                    deployment_epoch=deployment_epoch,
                    plan=config.primary,
                    user_data_path=user_data_path,
                )
                mode = config.primary.mode
            except CapacityUnavailable as exc:
                if not config.paid_enabled:
                    raise RecoveryError(
                        "A1 capacity unavailable and paid fallback is disabled"
                    ) from exc
                if paid_instance_count(rows) >= config.paid_max_concurrent:
                    raise RecoveryError(
                        "paid fallback concurrency limit reached"
                    ) from exc
                candidate_id = oci.launch(
                    config,
                    incident,
                    deployment_id=deployment_id,
                    deployment_epoch=deployment_epoch,
                    plan=config.fallback,
                    user_data_path=user_data_path,
                )
                mode = config.fallback.mode

        previous_instance_id = switch_volume(oci, config, candidate_id)
        if previous_instance_id is None:
            previous_instance_id = rollback_instance_id
        wait_for_candidate_ready(
            coordinator,
            deployment_id=deployment_id,
            deployment_epoch=deployment_epoch,
            timeout_seconds=config.candidate_timeout_seconds,
        )
        committed = commit_authority_safely(
            coordinator,
            deployment_id=deployment_id,
            deployment_epoch=deployment_epoch,
            expected_active_epoch=incident.expected_active_epoch,
        )
        if (
            previous_instance_id
            and previous_instance_id != candidate_id
        ):
            try:
                oci.terminate(previous_instance_id)
            except Exception as retire_error:
                print(
                    "RECOVERY_RETIRE_WARNING: "
                    f"{type(retire_error).__name__}: {retire_error}"
                )
        return {
            "status": "committed",
            "instance_id": candidate_id,
            "deployment_id": deployment_id,
            "deployment_epoch": deployment_epoch,
            "mode": mode,
            "leader_id": committed.get("leader_id"),
        }
    except CommitOutcomeUnknown:
        print(
            "RECOVERY_COMMIT_UNCERTAIN: leaving candidate and durable volume "
            "in place for coordinator reconciliation"
        )
        raise
    except Exception:
        if candidate_id:
            try:
                rollback_volume(
                    oci,
                    config,
                    candidate_id=candidate_id,
                    previous_instance_id=previous_instance_id,
                )
            except Exception as rollback_error:
                print(
                    "RECOVERY_ROLLBACK_WARNING: "
                    f"{type(rollback_error).__name__}: {rollback_error}"
                )
            try:
                oci.terminate(candidate_id)
            except Exception as terminate_error:
                print(
                    "RECOVERY_TERMINATE_WARNING: "
                    f"{type(terminate_error).__name__}: {terminate_error}"
                )
        try:
            coordinator.abort(
                deployment_id=deployment_id,
                deployment_epoch=deployment_epoch,
            )
        except Exception as abort_error:
            print(
                "RECOVERY_ABORT_WARNING: "
                f"{type(abort_error).__name__}: {abort_error}"
            )
        raise
    finally:
        if user_data_path is not None:
            user_data_path.unlink(missing_ok=True)


def cleanup_expired_paid(
    config: RecoveryConfig,
    oci: OciCli,
    *,
    now: datetime | None = None,
) -> list[str]:
    current = now or datetime.now(UTC)
    terminated: list[str] = []
    for row in oci.list_instances(config):
        tags = _tags(row)
        if tags.get("KatchaRecoveryMode") != "paid-fallback":
            continue
        if _state(row) in {"TERMINATED", "TERMINATING"}:
            continue
        expires_raw = tags.get("KatchaExpiresAt", "")
        if not expires_raw:
            raise RecoveryError(
                f"paid fallback instance {_instance_id(row)} is missing KatchaExpiresAt"
            )
        expires = datetime.fromisoformat(expires_raw.replace("Z", "+00:00"))
        if expires > current:
            continue
        instance_id = _instance_id(row)
        oci.softstop(instance_id)
        oci.terminate(instance_id)
        terminated.append(instance_id)
    return terminated


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=["recover", "cleanup"],
        nargs="?",
        default="recover",
    )
    parser.add_argument("--event", type=Path)
    args = parser.parse_args()
    try:
        config = RecoveryConfig.from_env(
            require_external_compute=args.action != "cleanup"
        )
        oci = OciCli()
        if args.action == "cleanup":
            result: object = {
                "status": "cleanup_complete",
                "terminated": cleanup_expired_paid(config, oci),
            }
        else:
            if args.event is None:
                raise RecoveryError("--event is required for recovery")
            result = recover(args.event, config, oci)
    except RecoveryError as exc:
        print(f"RECOVERY_FAILED: {exc}")
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
