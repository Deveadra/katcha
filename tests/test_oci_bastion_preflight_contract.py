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


def test_bastion_session_uses_ephemeral_rsa_key_and_target_key_stays_separate() -> None:
    script = (ROOT / "scripts" / "bootstrap-acquired-oci-host.sh").read_text(
        encoding="utf-8"
    )

    assert "ssh-keygen -q -t rsa -b 3072" in script
    assert '--ssh-public-key-file "$session_public_key"' in script
    assert '-i "$session_private_key"' in script
    assert "PubkeyAcceptedAlgorithms=+ssh-rsa" in script
    assert 'ssh-keygen -y -f "$SSH_PRIVATE_KEY_FILE"' in script
    assert "awk '{print $1 \" \" $2}'" in script
    assert "SSH private key does not match Katcha target public key" in script
    assert 'rm -rf "$session_key_dir"' in script

    target_ssh = script.split('log "Waiting for the private SSH endpoint through Bastion"', 1)[1]
    assert '-i "$SSH_PRIVATE_KEY_FILE"' in target_ssh
