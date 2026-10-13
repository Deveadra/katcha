"""Composition root cannot silently create replacement search evidence."""

from types import SimpleNamespace

import pytest

from katcha.services import editorial_media_preparation as preparation


def test_runtime_requires_postgres_and_never_creates_sqlite(tmp_path, monkeypatch):
    from sqlalchemy import create_engine

    settings = SimpleNamespace(
        source_search_recovery_generation=1, source_search_dispatch_enabled=False, work_dir=tmp_path
    )
    monkeypatch.setattr(preparation, "get_settings", lambda: settings)
    monkeypatch.setattr(preparation, "engine", create_engine("sqlite://"))
    preparation.runtime.cache_clear()
    with pytest.raises(preparation.EditorialConflict, match="Restore and verify"):
        preparation.runtime()
    assert not (tmp_path / "source-search-ledger.sqlite").exists()
    preparation.runtime.cache_clear()


def test_runtime_does_not_expose_database_connection_failure(tmp_path, monkeypatch):
    settings = SimpleNamespace(
        source_search_recovery_generation=1, source_search_dispatch_enabled=False, work_dir=tmp_path
    )

    def unavailable(*args, **kwargs):
        raise RuntimeError("postgres://private-user:secret@private-host")

    monkeypatch.setattr(preparation, "get_settings", lambda: settings)
    monkeypatch.setattr(preparation, "PostgresSearchJobLedger", unavailable)
    preparation.runtime.cache_clear()
    with pytest.raises(preparation.EditorialConflict) as exc:
        preparation.runtime()
    assert "secret" not in str(exc.value) and "Restore and verify" in str(exc.value)
    assert list(tmp_path.iterdir()) == []
    preparation.runtime.cache_clear()


def test_search_config_dispatch_defaults_closed():
    from katcha.config import Settings

    settings = Settings(_env_file=None)
    assert settings.source_search_dispatch_enabled is False
    assert settings.source_search_recovery_generation == 0
