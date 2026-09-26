import subprocess
import sys
import textwrap


def test_session_scope_registers_cross_domain_model_metadata() -> None:
    code = textwrap.dedent(
        """
        from sqlalchemy.orm import configure_mappers

        import katcha.db as db
        from katcha.short_episode_models import ShortEpisode  # noqa: F401

        assert "trend_opportunities" not in db.Base.metadata.tables

        class DummySession:
            def commit(self) -> None:
                pass

            def rollback(self) -> None:
                pass

            def close(self) -> None:
                pass

        db.SessionLocal = DummySession

        with db.session_scope():
            pass

        assert "trend_opportunities" in db.Base.metadata.tables
        configure_mappers()
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
