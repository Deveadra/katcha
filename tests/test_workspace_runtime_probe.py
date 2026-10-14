import pytest

from katcha.ops import workspace_runtime_probe as probe


def test_runtime_probe_decodes_and_probes_real_synthetic_video():
    probe.probe_media()


@pytest.mark.parametrize("missing", ["ffmpeg", "ffprobe"])
def test_runtime_probe_rejects_missing_media_tool(monkeypatch, missing):
    monkeypatch.setattr(probe.shutil, "which", lambda name: None if name == missing else name)
    with pytest.raises(probe.RuntimeProbeError, match=f"missing media executable: {missing}"):
        probe.probe_media()


def test_asset_probe_does_not_process_startup_handoffs(monkeypatch):
    from katcha.api import main

    async def forbidden_startup():
        pytest.fail("offline image acceptance must not process handoffs or access the DB")

    monkeypatch.setattr(main, "_process_pending_handoffs_on_startup", forbidden_startup)
    assert probe.probe_workspace_assets() > 20
