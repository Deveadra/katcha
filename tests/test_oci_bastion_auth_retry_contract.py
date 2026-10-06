from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_bastion_session_retries_until_key_is_usable() -> None:
    script = (ROOT / "scripts" / "bootstrap-acquired-oci-host.sh").read_text(
        encoding="utf-8"
    )
    assert 'Waiting for Bastion session SSH authentication to propagate' in script
    assert 'for attempt in $(seq 1 24)' in script
    assert '-F /dev/null' in script
    assert 'IdentityAgent=none' in script
    assert 'HostKeyAlgorithms=+ssh-rsa' in script
    assert 'PubkeyAcceptedAlgorithms=+ssh-rsa' in script
    assert 'propagation timeout' in script
    assert 'bastion session delete' in script
