"""Portable user-service contracts; activation and credentials remain operator actions."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "deploy/acquisition/katcha-acquisition-workstation.service"


def test_workstation_service_uses_file_credentials_and_existing_worker():
    text = UNIT.read_text()
    credentials = [line for line in text.splitlines() if line.startswith("LoadCredential=")]
    assert credentials == [
        f"LoadCredential={name}:%h/.config/katcha-acquisition/credentials/{name}"
        for name in ("worker-token",)
    ]
    assert "Environment=KATCHA_ACQUISITION_TOKEN_FILE=%d/worker-token" in text
    access = (UNIT.parent / "workstation-access.conf").read_text()
    for name, suffix in (("cf-access-id", "ID"), ("cf-access-secret", "SECRET")):
        assert f"LoadCredential={name}:%h/.config/katcha-acquisition/credentials/{name}" in access
        assert f"Environment=KATCHA_ACQUISITION_CF_ACCESS_{suffix}_FILE=%d/{name}" in access
    assert "-m katcha.acquisition.worker" in text
    assert "--once" not in text
    assert "Environment=KATCHA_ACQUISITION_STATE_DIR=%h/.local/state/katcha-acquisition" in text
    assert "Restart=always" in text and "UMask=0077" in text
    assert "WantedBy=default.target" in text
    for forbidden in ("sudo", "bastion", "22022", "DynamicUser=", "User=root", "ExecStartPre="):
        assert forbidden not in text


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="systemd unavailable")
def test_workstation_service_passes_systemd_parser(tmp_path):
    # verify resolves ExecStart; map only that executable to installed Python.
    unit = tmp_path / UNIT.name
    unit.write_text(UNIT.read_text().replace(
        "%h/.local/share/katcha-acquisition/.venv/bin/python", shutil.which("python3")
    ))
    dropins = tmp_path / (UNIT.name + ".d")
    dropins.mkdir()
    for name in ("workstation-access.conf", "workstation-private-ca.conf"):
        (dropins / name).write_text((UNIT.parent / name).read_text())
    result = subprocess.run(
        ["systemd-analyze", "verify", str(unit)], capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
