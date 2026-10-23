"""Local diagnostics distinguish configured capacity from verified service readiness."""

import importlib.util
import json
from pathlib import Path

import pytest

from katcha.config import Settings
from katcha.services.retrieval_diagnostics import retrieval_capabilities


@pytest.mark.parametrize("enabled,status", [(False, "not_enabled"), (True, "unknown")])
def test_real_capacity_limits_and_configuration_do_not_claim_live_readiness(
    monkeypatch, enabled, status
):
    from katcha import config
    from katcha.editorial.media_intake.scene_index import MAX_INDEX_BYTES
    from katcha.editorial.media_intake.scene_worker import MAX_FRAMES, MAX_SECONDS

    settings = Settings(
        _env_file=None,
        semantic_matching_dispatch_enabled=enabled,
        openai_api_key="secret-fixture-value",
    )
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    monkeypatch.setattr("katcha.services.browser_client.get_settings", lambda: settings)
    capabilities = retrieval_capabilities()
    index = capabilities["measured_scene_index"]
    assert index["max_input_bytes"] == MAX_INDEX_BYTES == 200 * 1024**2
    assert index["max_duration_seconds"] == MAX_SECONDS == 600
    assert index["max_decoded_frames"] == MAX_FRAMES == 36000
    assert capabilities["remote_acquisition"]["configured_max_bytes"] > MAX_INDEX_BYTES
    inspection = capabilities["optional_semantic_inspection"]
    assert inspection["status"] == status
    assert inspection["max_shots"] == inspection["max_requirements"] == 8
    assert not inspection["live_provider_verified"]
    assert not capabilities["scene_verification"]["automatic_action_verification_available"]
    assert "secret-fixture-value" not in json.dumps(capabilities)


def test_source_preflight_never_promotes_file_presence_to_live_or_channel_proof():
    spec = importlib.util.spec_from_file_location(
        "autonomous_e2e_preflight", "scripts/autonomous_e2e_preflight.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    report = module.build_report({"fixture": True})
    assert report["program_acceptance"] == "not_assessed"
    assert report["deployed_sha"] is report["database_revision"] is None
    assert report["source_migration_heads"]
    assert report["source_migration_heads"] == sorted(set(report["source_migration_heads"]))
    for owner in report["owners"].values():
        assert owner["all_sources_present"], owner
        assert owner["live_status"] == "unknown"
    assert all(
        Path(module.ROOT / p).is_file()
        for owner in report["owners"].values()
        for p in owner["source_paths"]
    )


def test_migration_inventory_parses_merge_heads_without_executing_sources(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "autonomous_e2e_preflight", "scripts/autonomous_e2e_preflight.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    folder = tmp_path / "migrations/versions"
    folder.mkdir(parents=True)
    for name, revision, parent in [
        ("a", "a", None),
        ("b", "b", "a"),
        ("c", "c", "a"),
        ("d", "d", ("b", "c")),
    ]:
        (folder / f"{name}.py").write_text(
            f"raise RuntimeError('must not execute')\nrevision: str = {revision!r}\n"
            f"down_revision = {parent!r}\n"
        )
    assert module.migration_heads() == ["d"]
