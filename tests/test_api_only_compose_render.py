"""Render the real production Compose file with independent API release pins."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "deploy/docker-compose.production.yml"
EXAMPLE = ROOT / ".env.production.example"
BASE_SHA = "a" * 40
API_SHA = "b" * 40


def _render(tmp_path: Path, api_sha: str | None) -> dict:
    if not shutil.which("docker"):
        pytest.skip("Docker Compose v2 is unavailable in this environment")
    version = subprocess.run(
        ["docker", "compose", "version"], capture_output=True, text=True, check=False,
    )
    if version.returncode:
        pytest.skip("Docker Compose v2 is unavailable in this environment")

    fixture = EXAMPLE.read_text(encoding="utf-8").replace(
        "KATCHA_RELEASE_SHA=CHANGE_ME_EXACT_40_CHARACTER_GIT_SHA",
        f"KATCHA_RELEASE_SHA={BASE_SHA}",
    )
    if api_sha is not None:
        fixture += f"\nKATCHA_API_RELEASE_SHA={api_sha}\n"
    env_file = tmp_path / "production.env"
    env_file.write_text(fixture, encoding="utf-8")

    # The config command must never touch production; this fixture is isolated
    # and no secrets are present. Prevent inherited operator KATCHA_* settings.
    env = {k: v for k, v in os.environ.items() if not k.startswith("KATCHA_")}
    env.update({
        "KATCHA_ENV_FILE": str(env_file),
        "KATCHA_BACKUP_ENV_FILE": str(env_file),
        "KATCHA_RESTORE_ENV_FILE": str(env_file),
    })
    result = subprocess.run(
        [
            "docker", "compose", "--project-name", "katcha-production",
            "--env-file", str(env_file), "-f", str(COMPOSE),
            "config", "--no-env-resolution", "--format", "json",
        ],
        env=env, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["services"]


def test_api_override_only_changes_api_image_and_public_revision(
    tmp_path: Path,
) -> None:
    original = _render(tmp_path, None)
    updated = _render(tmp_path, API_SHA)

    assert original["api"]["image"] == f"ghcr.io/deveadra/katcha-control:{BASE_SHA}"
    assert updated["api"]["image"] == f"ghcr.io/deveadra/katcha-control:{API_SHA}"
    assert original["api"]["environment"]["KATCHA_RELEASE_SHA"] == BASE_SHA
    assert updated["api"]["environment"]["KATCHA_RELEASE_SHA"] == API_SHA

    # Every other service has exactly the same rendered configuration, so a
    # routine API-only Compose update must not retag or reconfigure workers.
    assert {k: v for k, v in updated.items() if k != "api"} == {
        k: v for k, v in original.items() if k != "api"
    }
    for name in ("worker", "intelligence-worker", "production-worker",
                 "renderer", "migrate"):
        assert updated[name]["image"].endswith(":" + BASE_SHA)
