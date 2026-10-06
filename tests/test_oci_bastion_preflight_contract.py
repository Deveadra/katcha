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


def test_bastion_bootstrap_polls_resources_instead_of_cli_waiters() -> None:
    script = (ROOT / "scripts" / "bootstrap-acquired-oci-host.sh").read_text(
        encoding="utf-8"
    )
    bastion_create = script.split("bastion bastion create", 1)[1].split(")", 1)[0]
    session_create = script.split("bastion session create-port-forwarding", 1)[1].split(")", 1)[0]

    assert "--wait-for-state" not in bastion_create
    assert "--wait-for-state" not in session_create
    assert "bastion bastion get --bastion-id" in script
    assert "bastion session get --session-id" in script
    assert 'bastion_state" == "FAILED"' in script
    assert 'session_state" == "FAILED"' in script
