import json
import zipfile
from pathlib import Path

from katcha.api.main import app
from katcha.services import external_edit
from katcha.services.external_edit import _invideo_instructions


def test_external_provider_routes_are_registered() -> None:
    paths = app.openapi()["paths"]
    expected = {
        "/v1/integrations/providers",
        "/v1/integrations/invideo/handoffs",
        "/v1/integrations/invideo/handoffs/{handoff_id}",
        "/v1/integrations/invideo/handoffs/{handoff_id}/manifest",
        "/v1/integrations/invideo/handoffs/{handoff_id}/package",
        "/v1/integrations/invideo/handoffs/{handoff_id}/output",
        "/v1/integrations/invideo/handoffs/{handoff_id}/adopt",
    }
    assert expected <= set(paths)
    assert "post" in paths["/v1/integrations/invideo/handoffs"]
    assert "post" in paths["/v1/integrations/invideo/handoffs/{handoff_id}/output"]
    assert "post" in paths["/v1/integrations/invideo/handoffs/{handoff_id}/adopt"]


def test_invideo_brief_keeps_katcha_as_source_of_truth() -> None:
    instructions = _invideo_instructions(
        brand={"brand_key": "ranksnaxx"},
        blueprint={"key": "persona_commentary"},
    )
    text = " ".join(instructions).lower()
    assert "source of truth" in text
    assert "do not publish directly" in text
    assert "brand.json" in text
    assert "editing-recipe.json" in text



def test_invideo_package_contains_frozen_edit_contracts(monkeypatch, tmp_path: Path) -> None:
    manifest = {
        "instructions": ["Use the supplied files."],
        "brand": {"key": "ranksnaxx", "version": 3},
        "editing_recipe": {"key": "persona_commentary", "version": 2},
        "script": {"style": "sarcastic", "narration": "Test narration"},
        "assets": [
            {
                "storage_key": "raw/source.mp4",
                "role": "source_video",
                "filename": "source.mp4",
                "content_type": "video/mp4",
            }
        ],
    }

    class Store:
        def download_file(self, key: str, destination: Path) -> None:
            assert key == "raw/source.mp4"
            destination.write_bytes(b"video")

    monkeypatch.setattr(external_edit, "handoff_manifest", lambda _handoff_id: manifest)
    monkeypatch.setattr(external_edit, "ObjectStore", Store)
    monkeypatch.setattr(
        external_edit.tempfile,
        "mkdtemp",
        lambda **_: str(tmp_path / "handoff"),
    )
    (tmp_path / "handoff").mkdir()

    archive = external_edit.build_handoff_zip(
        external_edit.uuid.UUID("11111111-1111-4111-8111-111111111111")
    )

    with zipfile.ZipFile(archive) as handle:
        names = set(handle.namelist())
        assert {
            "manifest.json",
            "README.txt",
            "brand.json",
            "editing-recipe.json",
            "script.json",
            "assets/source.mp4",
        } <= names
        assert json.loads(handle.read("brand.json"))["key"] == "ranksnaxx"
        assert json.loads(handle.read("editing-recipe.json"))["version"] == 2
