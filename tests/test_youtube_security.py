import pytest
from cryptography.fernet import Fernet

from katcha.config import Settings
from katcha.security.secrets import (
    SecretConfigurationError,
    decrypt_secret,
    encrypt_secret,
)


def test_secret_round_trip() -> None:
    key = Fernet.generate_key().decode("ascii")
    settings = Settings(_env_file=None, credential_encryption_key=key)

    encrypted = encrypt_secret("refresh-token-value", settings)

    assert encrypted != "refresh-token-value"
    assert decrypt_secret(encrypted, settings) == "refresh-token-value"


def test_secret_requires_valid_key() -> None:
    settings = Settings(_env_file=None, credential_encryption_key="not-a-fernet-key")

    with pytest.raises(SecretConfigurationError):
        encrypt_secret("value", settings)
