from __future__ import annotations

from katcha.ops.disaster_recovery_validate import validate


def _production() -> dict[str, str]:
    return {
        "KATCHA_S3_BUCKET": "katcha-media-prod",
        "KATCHA_S3_ACCESS_KEY": "media-access-key",
    }


def _backup() -> dict[str, str]:
    return {
        "KATCHA_BACKUP_R2_ENDPOINT_URL": (
            "https://abc123.r2.cloudflarestorage.com"
        ),
        "KATCHA_BACKUP_R2_ACCESS_KEY": "backup-writer-access-key",
        "KATCHA_BACKUP_R2_SECRET_KEY": "backup-writer-secret",
        "KATCHA_BACKUP_R2_BUCKET": "katcha-backup-prod",
        "KATCHA_BACKUP_R2_PREFIX": "postgres",
        "KATCHA_BACKUP_R2_REGION": "auto",
        "KATCHA_BACKUP_R2_FORCE_PATH_STYLE": "false",
    }


def _restore() -> dict[str, str]:
    return {
        "KATCHA_RESTORE_R2_ENDPOINT_URL": (
            "https://abc123.r2.cloudflarestorage.com"
        ),
        "KATCHA_RESTORE_R2_ACCESS_KEY": "backup-reader-access-key",
        "KATCHA_RESTORE_R2_SECRET_KEY": "backup-reader-secret",
        "KATCHA_RESTORE_R2_BUCKET": "katcha-backup-prod",
        "KATCHA_RESTORE_R2_PREFIX": "postgres",
        "KATCHA_RESTORE_R2_REGION": "auto",
        "KATCHA_RESTORE_R2_FORCE_PATH_STYLE": "false",
    }


def test_disaster_recovery_credentials_are_separated() -> None:
    assert validate(_production(), _backup(), _restore()) == []


def test_disaster_recovery_rejects_shared_media_bucket_and_keys() -> None:
    production = _production()
    backup = _backup()
    restore = _restore()
    backup["KATCHA_BACKUP_R2_BUCKET"] = production["KATCHA_S3_BUCKET"]
    backup["KATCHA_BACKUP_R2_ACCESS_KEY"] = production["KATCHA_S3_ACCESS_KEY"]
    restore["KATCHA_RESTORE_R2_ACCESS_KEY"] = backup["KATCHA_BACKUP_R2_ACCESS_KEY"]

    errors = validate(production, backup, restore)

    assert any("bucket separate from runtime media" in error for error in errors)
    assert any("backup writer credentials must differ" in error for error in errors)
    assert any("writer and restore reader credentials" in error for error in errors)


def test_disaster_recovery_rejects_mismatched_restore_target() -> None:
    backup = _backup()
    restore = _restore()
    restore["KATCHA_RESTORE_R2_BUCKET"] = "different-bucket"
    restore["KATCHA_RESTORE_R2_PREFIX"] = "other-prefix"

    errors = validate(_production(), backup, restore)

    assert any("same disaster-backup bucket" in error for error in errors)
    assert any("same backup prefix" in error for error in errors)
