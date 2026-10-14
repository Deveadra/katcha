import json
from types import SimpleNamespace

import pytest

from katcha.ops import workspace_runtime_probe as probe


def test_runtime_probe_decodes_synthetic_video_with_fixture_ffprobe(monkeypatch):
    """Unit fixture for CLI output; built-image CI separately runs real ffprobe."""
    import av

    monkeypatch.setattr(probe.shutil, "which", lambda name: name)

    def ffprobe_fixture(argv, **kwargs):
        assert argv[:2] == ["ffprobe", "-v"]
        assert kwargs["timeout"] == 10
        assert kwargs["check"] is True
        with av.open(argv[-1], "r") as source:
            stream = source.streams.video[0]
            return SimpleNamespace(stdout=json.dumps({"streams": [{"width": stream.width}]}))

    monkeypatch.setattr(probe.subprocess, "run", ffprobe_fixture)
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
