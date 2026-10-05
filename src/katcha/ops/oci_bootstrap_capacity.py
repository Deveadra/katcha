from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class BootstrapError(RuntimeError):
    pass


class CapacityUnavailable(BootstrapError):
    pass


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        if default is not None:
            return default
        raise BootstrapError(f"missing required bootstrap setting: {name}")
    return value.strip()


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().casefold() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class BootstrapTarget:
    availability_domain: str
    subnet_id: str
    volume_name: str


@dataclass(frozen=True, slots=True)
class BootstrapConfig:
    compartment_id: str
    image_id: str
    ssh_public_key: str
    targets: tuple[BootstrapTarget, ...]
    primary_volume_id: str
    instance_name: str = "katcha-prod-primary-001"
    shape: str = "VM.Standard.A1.Flex"
    ocpus: float = 2.0
    memory_gb: float = 12.0
    boot_volume_size_gb: int = 50
    data_volume_size_gb: int = 50
    data_volume_vpus_per_gb: int = 10
    data_volume_device_path: str = "/dev/oracleoci/oraclevdb"

    @classmethod
    def from_env(cls) -> BootstrapConfig:
        primary_ad = _env("KATCHA_OCI_AVAILABILITY_DOMAIN")
        primary_subnet = _env("KATCHA_OCI_SUBNET_ID")
        try:
            raw_targets = json.loads(
                _env("KATCHA_OCI_CROSS_AD_TARGETS_JSON", "[]")
            )
        except json.JSONDecodeError as exc:
            raise BootstrapError(
                "KATCHA_OCI_CROSS_AD_TARGETS_JSON must be valid JSON"
            ) from exc
        if not isinstance(raw_targets, list):
            raise BootstrapError(
                "KATCHA_OCI_CROSS_AD_TARGETS_JSON must be a JSON array"
            )

        targets = [
            BootstrapTarget(
                availability_domain=primary_ad,
                subnet_id=primary_subnet,
                volume_name="katcha-prod-data",
            )
        ]
        for row in raw_targets:
            if not isinstance(row, dict):
                raise BootstrapError(
                    "each cross-AD bootstrap target must be a JSON object"
                )
            availability_domain = str(
                row.get("availability_domain") or ""
            ).strip()
            subnet_id = str(row.get("subnet_id") or "").strip()
            if not availability_domain or not subnet_id:
                raise BootstrapError(
                    "each cross-AD bootstrap target requires "
                    "availability_domain and subnet_id"
                )
            match = re.search(r"AD-(\d+)$", availability_domain, re.IGNORECASE)
            suffix = match.group(1) if match else str(len(targets) + 1)
            targets.append(
                BootstrapTarget(
                    availability_domain=availability_domain,
                    subnet_id=subnet_id,
                    volume_name=f"katcha-prod-data-ad{suffix}",
                )
            )

        seen: set[tuple[str, str]] = set()
        for target in targets:
            key = (target.availability_domain, target.subnet_id)
            if key in seen:
                raise BootstrapError("bootstrap targets must be unique")
            seen.add(key)

        shape = _env("KATCHA_OCI_PRIMARY_SHAPE", "VM.Standard.A1.Flex")
        ocpus = float(_env("KATCHA_OCI_PRIMARY_OCPUS", "2"))
        memory_gb = float(_env("KATCHA_OCI_PRIMARY_MEMORY_GB", "12"))
        if shape != "VM.Standard.A1.Flex" or ocpus != 2 or memory_gb != 12:
            raise BootstrapError(
                "initial capacity poller is intentionally pinned to "
                "VM.Standard.A1.Flex at 2 OCPU / 12 GB"
            )
        if _bool_env("KATCHA_OCI_ASSIGN_PUBLIC_IP", False):
            raise BootstrapError(
                "initial Katcha bootstrap refuses to assign a public IP"
            )

        ssh_public_key = _env("KATCHA_OCI_SSH_PUBLIC_KEY")
        if "\n" in ssh_public_key or not ssh_public_key.startswith("ssh-"):
            raise BootstrapError(
                "KATCHA_OCI_SSH_PUBLIC_KEY must contain one OpenSSH public key"
            )

        return cls(
            compartment_id=_env("KATCHA_OCI_COMPARTMENT_ID"),
            image_id=_env("KATCHA_OCI_PRIMARY_IMAGE_ID"),
            ssh_public_key=ssh_public_key,
            targets=tuple(targets),
            primary_volume_id=os.environ.get(
                "KATCHA_OCI_DATA_VOLUME_ID", ""
            ).strip(),
            shape=shape,
            ocpus=ocpus,
            memory_gb=memory_gb,
            data_volume_size_gb=int(
                _env("KATCHA_OCI_CROSS_AD_DATA_VOLUME_SIZE_GB", "50")
            ),
            data_volume_device_path=_env(
                "KATCHA_OCI_DATA_VOLUME_DEVICE_PATH",
                "/dev/oracleoci/oraclevdb",
            ),
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
            raise BootstrapError(
                f"OCI command failed ({' '.join(command[:4])}): "
                f"{message[:1000]}"
            ) from exc
        if not completed.stdout.strip():
            return {}
        payload = json.loads(completed.stdout)
        return payload if isinstance(payload, dict) else {"data": payload}


def _field(row: dict[str, Any], hyphen: str, camel: str = "") -> Any:
    if hyphen in row:
        return row[hyphen]
    if camel and camel in row:
        return row[camel]
    return None


def _state(row: dict[str, Any]) -> str:
    return str(_field(row, "lifecycle-state", "lifecycleState") or "")


def _tags(row: dict[str, Any]) -> dict[str, str]:
    value = _field(row, "freeform-tags", "freeformTags") or {}
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items()}


class BootstrapProvisioner:
    def __init__(
        self,
        config: BootstrapConfig,
        client: OciCli | None = None,
        *,
        sleep: Any = time.sleep,
    ) -> None:
        self.config = config
        self.client = client or OciCli()
        self.sleep = sleep

    def _instances(self) -> list[dict[str, Any]]:
        rows = self.client.run(
            [
                "compute",
                "instance",
                "list",
                "--compartment-id",
                self.config.compartment_id,
                "--all",
            ]
        ).get("data", [])
        return list(rows) if isinstance(rows, list) else []

    def _find_existing_instance(self) -> dict[str, Any] | None:
        matches = [
            row
            for row in self._instances()
            if str(_field(row, "display-name", "displayName") or "")
            == self.config.instance_name
            and _state(row) not in {"TERMINATED", "TERMINATING"}
        ]
        if len(matches) > 1:
            raise BootstrapError(
                "multiple active bootstrap instances share the production name"
            )
        return matches[0] if matches else None

    def _target_for_instance(
        self, instance: dict[str, Any]
    ) -> BootstrapTarget:
        ad = str(
            _field(instance, "availability-domain", "availabilityDomain") or ""
        )
        matches = [
            target
            for target in self.config.targets
            if target.availability_domain == ad
        ]
        if len(matches) != 1:
            raise BootstrapError(
                f"existing bootstrap instance is in unexpected AD: {ad}"
            )
        target = matches[0]
        tagged_subnet = _tags(instance).get("KatchaSubnetId", "").strip()
        if tagged_subnet and tagged_subnet != target.subnet_id:
            raise BootstrapError(
                "existing bootstrap instance has an unexpected subnet tag"
            )
        return target

    def _validate_instance(self, instance: dict[str, Any]) -> None:
        if str(instance.get("shape") or "") != self.config.shape:
            raise BootstrapError("existing bootstrap instance has wrong shape")
        shape_config = (
            _field(instance, "shape-config", "shapeConfig") or {}
        )
        ocpus = float(shape_config.get("ocpus") or 0)
        memory = float(
            shape_config.get("memory-in-gbs")
            or shape_config.get("memoryInGBs")
            or 0
        )
        if ocpus != self.config.ocpus or memory != self.config.memory_gb:
            raise BootstrapError(
                "existing bootstrap instance is not 2 OCPU / 12 GB"
            )
        state = _state(instance)
        if state in {"PROVISIONING", "STARTING"}:
            instance_id = str(instance.get("id") or "")
            refreshed = self.client.run(
                [
                    "compute",
                    "instance",
                    "get",
                    "--instance-id",
                    instance_id,
                    "--wait-for-state",
                    "RUNNING",
                    "--max-wait-seconds",
                    "1200",
                ]
            ).get("data", {})
            state = _state(refreshed) if isinstance(refreshed, dict) else ""
        if state != "RUNNING":
            raise BootstrapError(
                f"existing bootstrap instance is not RUNNING: {state}"
            )

    def _launch(self, target: BootstrapTarget) -> dict[str, Any]:
        tags = {
            "KatchaRole": "control-plane",
            "KatchaBootstrap": "true",
            "KatchaPaidFallback": "false",
            "KatchaSubnetId": target.subnet_id,
        }
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix="katcha-oci-key-",
            delete=False,
        ) as handle:
            handle.write(self.config.ssh_public_key + "\n")
            key_path = Path(handle.name)
        key_path.chmod(0o600)
        try:
            result = self.client.run(
                [
                    "compute",
                    "instance",
                    "launch",
                    "--compartment-id",
                    self.config.compartment_id,
                    "--availability-domain",
                    target.availability_domain,
                    "--display-name",
                    self.config.instance_name,
                    "--hostname-label",
                    self.config.instance_name,
                    "--shape",
                    self.config.shape,
                    "--shape-config",
                    json.dumps(
                        {
                            "ocpus": self.config.ocpus,
                            "memoryInGBs": self.config.memory_gb,
                        },
                        separators=(",", ":"),
                    ),
                    "--image-id",
                    self.config.image_id,
                    "--subnet-id",
                    target.subnet_id,
                    "--assign-public-ip",
                    "false",
                    "--boot-volume-size-in-gbs",
                    str(self.config.boot_volume_size_gb),
                    "--ssh-authorized-keys-file",
                    str(key_path),
                    "--freeform-tags",
                    json.dumps(tags, separators=(",", ":")),
                    "--wait-for-state",
                    "RUNNING",
                    "--max-wait-seconds",
                    "1200",
                ]
            )
        finally:
            key_path.unlink(missing_ok=True)
        instance = result.get("data", {})
        if not isinstance(instance, dict) or not instance.get("id"):
            raise BootstrapError("OCI launch returned no instance OCID")
        return instance

    def _reconcile_uncertain_launch(self) -> dict[str, Any] | None:
        for _ in range(5):
            existing = self._find_existing_instance()
            if existing is not None:
                return existing
            self.sleep(3)
        return None

    def _ensure_private_vnic(
        self, instance_id: str, target: BootstrapTarget
    ) -> str:
        rows = self.client.run(
            [
                "compute",
                "vnic-attachment",
                "list",
                "--compartment-id",
                self.config.compartment_id,
                "--instance-id",
                instance_id,
                "--all",
            ]
        ).get("data", [])
        attachments = [
            row
            for row in rows
            if isinstance(row, dict)
            and _state(row) not in {"DETACHED", "DETACHING"}
        ]
        if len(attachments) != 1:
            raise BootstrapError(
                "bootstrap instance must have exactly one active primary VNIC"
            )
        vnic_id = str(
            _field(attachments[0], "vnic-id", "vnicId") or ""
        )
        if not vnic_id:
            raise BootstrapError("primary VNIC attachment is missing a VNIC OCID")
        vnic = self.client.run(
            ["network", "vnic", "get", "--vnic-id", vnic_id]
        ).get("data", {})
        if not isinstance(vnic, dict):
            raise BootstrapError("OCI VNIC lookup returned invalid data")
        public_ip = _field(vnic, "public-ip", "publicIp")
        if public_ip:
            raise BootstrapError(
                "bootstrap instance unexpectedly has a public IP"
            )
        subnet_id = str(_field(vnic, "subnet-id", "subnetId") or "")
        if subnet_id != target.subnet_id:
            raise BootstrapError("bootstrap instance is in the wrong subnet")
        return str(_field(vnic, "private-ip", "privateIp") or "")

    def _volume(self, target: BootstrapTarget) -> dict[str, Any] | None:
        if (
            target == self.config.targets[0]
            and self.config.primary_volume_id
        ):
            volume = self.client.run(
                [
                    "bv",
                    "volume",
                    "get",
                    "--volume-id",
                    self.config.primary_volume_id,
                ]
            ).get("data", {})
            if not isinstance(volume, dict):
                raise BootstrapError("primary volume lookup returned invalid data")
            return volume

        rows = self.client.run(
            [
                "bv",
                "volume",
                "list",
                "--compartment-id",
                self.config.compartment_id,
                "--all",
            ]
        ).get("data", [])
        matches = [
            row
            for row in rows
            if isinstance(row, dict)
            and str(_field(row, "display-name", "displayName") or "")
            == target.volume_name
            and str(
                _field(row, "availability-domain", "availabilityDomain") or ""
            )
            == target.availability_domain
            and _state(row) not in {"TERMINATED", "TERMINATING"}
        ]
        if len(matches) > 1:
            raise BootstrapError(
                f"multiple active volumes named {target.volume_name}"
            )
        return matches[0] if matches else None

    def _ensure_volume(self, target: BootstrapTarget) -> dict[str, Any]:
        volume = self._volume(target)
        if volume is None:
            result = self.client.run(
                [
                    "bv",
                    "volume",
                    "create",
                    "--compartment-id",
                    self.config.compartment_id,
                    "--availability-domain",
                    target.availability_domain,
                    "--display-name",
                    target.volume_name,
                    "--size-in-gbs",
                    str(self.config.data_volume_size_gb),
                    "--vpus-per-gb",
                    str(self.config.data_volume_vpus_per_gb),
                    "--wait-for-state",
                    "AVAILABLE",
                    "--max-wait-seconds",
                    "1200",
                ]
            )
            volume = result.get("data", {})
        if not isinstance(volume, dict) or not volume.get("id"):
            raise BootstrapError("durable volume provisioning returned no OCID")
        ad = str(
            _field(volume, "availability-domain", "availabilityDomain") or ""
        )
        if ad != target.availability_domain:
            raise BootstrapError("durable volume is in the wrong AD")
        if _state(volume) not in {"AVAILABLE", "IN_USE"}:
            raise BootstrapError(
                f"durable volume has unexpected state: {_state(volume)}"
            )
        return volume

    def _ensure_attachment(
        self, instance_id: str, volume_id: str
    ) -> str:
        rows = self.client.run(
            [
                "compute",
                "volume-attachment",
                "list",
                "--compartment-id",
                self.config.compartment_id,
                "--volume-id",
                volume_id,
                "--all",
            ]
        ).get("data", [])
        active = [
            row
            for row in rows
            if isinstance(row, dict)
            and _state(row) not in {"DETACHED", "DETACHING"}
        ]
        for attachment in active:
            attached_instance = str(
                _field(attachment, "instance-id", "instanceId") or ""
            )
            if attached_instance != instance_id:
                raise BootstrapError(
                    "durable volume is already attached to another instance"
                )
            attachment_id = str(attachment.get("id") or "")
            if _state(attachment) != "ATTACHED":
                result = self.client.run(
                    [
                        "compute",
                        "volume-attachment",
                        "get",
                        "--volume-attachment-id",
                        attachment_id,
                        "--wait-for-state",
                        "ATTACHED",
                        "--max-wait-seconds",
                        "600",
                    ]
                ).get("data", {})
                if not isinstance(result, dict) or _state(result) != "ATTACHED":
                    raise BootstrapError(
                        "existing durable-volume attachment did not become ATTACHED"
                    )
            return attachment_id

        result = self.client.run(
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
                self.config.data_volume_device_path,
                "--wait-for-state",
                "ATTACHED",
                "--max-wait-seconds",
                "600",
            ]
        ).get("data", {})
        attachment_id = str(result.get("id") or "") if isinstance(result, dict) else ""
        if not attachment_id:
            raise BootstrapError("durable-volume attach returned no attachment OCID")
        return attachment_id

    def _record_volume(
        self,
        instance: dict[str, Any],
        target: BootstrapTarget,
        volume_id: str,
    ) -> None:
        instance_id = str(instance.get("id") or "")
        tags = _tags(instance)
        tags.update(
            {
                "KatchaRole": "control-plane",
                "KatchaBootstrap": "true",
                "KatchaPaidFallback": "false",
                "KatchaSubnetId": target.subnet_id,
                "KatchaDataVolumeId": volume_id,
            }
        )
        self.client.run(
            [
                "compute",
                "instance",
                "update",
                "--instance-id",
                instance_id,
                "--freeform-tags",
                json.dumps(tags, separators=(",", ":")),
                "--force",
            ]
        )

    def _finish(
        self, instance: dict[str, Any], target: BootstrapTarget
    ) -> dict[str, Any]:
        self._validate_instance(instance)
        instance_id = str(instance.get("id") or "")
        private_ip = self._ensure_private_vnic(instance_id, target)
        volume = self._ensure_volume(target)
        volume_id = str(volume.get("id") or "")
        attachment_id = self._ensure_attachment(instance_id, volume_id)
        self._record_volume(instance, target, volume_id)
        return {
            "status": "acquired",
            "instance_id": instance_id,
            "availability_domain": target.availability_domain,
            "subnet_id": target.subnet_id,
            "private_ip": private_ip,
            "volume_id": volume_id,
            "attachment_id": attachment_id,
            "shape": self.config.shape,
            "ocpus": self.config.ocpus,
            "memory_gb": self.config.memory_gb,
        }

    def acquire(self) -> dict[str, Any]:
        existing = self._find_existing_instance()
        if existing is not None:
            target = self._target_for_instance(existing)
            result = self._finish(existing, target)
            result["reused_instance"] = True
            return result

        attempted: list[str] = []
        for target in self.config.targets:
            attempted.append(target.availability_domain)
            try:
                instance = self._launch(target)
            except CapacityUnavailable:
                continue
            except BootstrapError:
                instance = self._reconcile_uncertain_launch()
                if instance is None:
                    raise
                target = self._target_for_instance(instance)
            result = self._finish(instance, target)
            result["reused_instance"] = False
            result["attempted"] = attempted
            return result

        return {
            "status": "capacity-unavailable",
            "shape": self.config.shape,
            "ocpus": self.config.ocpus,
            "memory_gb": self.config.memory_gb,
            "attempted": attempted,
        }


def main() -> int:
    try:
        result = BootstrapProvisioner(BootstrapConfig.from_env()).acquire()
    except BootstrapError as exc:
        print(f"katcha OCI bootstrap failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
