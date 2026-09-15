from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from katcha.config import Settings, get_settings


class SecretConfigurationError(RuntimeError):
    pass


def _fernet(settings: Settings | None = None) -> Fernet:
    settings = settings or get_settings()
    if not settings.credential_encryption_key:
        raise SecretConfigurationError(
            "KATCHA_CREDENTIAL_ENCRYPTION_KEY is required for encrypted integrations"
        )
    try:
        return Fernet(settings.credential_encryption_key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise SecretConfigurationError(
            "KATCHA_CREDENTIAL_ENCRYPTION_KEY must be a valid Fernet key"
        ) from exc


def encrypt_secret(value: str, settings: Settings | None = None) -> str:
    if not value:
        raise ValueError("secret value cannot be empty")
    return _fernet(settings).encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(value: str, settings: Settings | None = None) -> str:
    try:
        return _fernet(settings).decrypt(value.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise SecretConfigurationError("encrypted secret could not be decrypted") from exc
