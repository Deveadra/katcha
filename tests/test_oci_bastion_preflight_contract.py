from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_bastion_preflight_accepts_empty_successful_list() -> None:
    script = (ROOT / "scripts" / "bootstrap-acquired-oci-host.sh").read_text(
        encoding="utf-8"
    )
    assert "SUPPRESS_LABEL_WARNING=True" in script
    assert "if bastion_list=" in script
    assert "Treat exit status, not stdout length, as success." in script
    assert "notauthorizedornotfound" in script
    assert "|| true" not in script.split("bastion_list=", 1)[1].split("bastion_id=", 1)[0]
