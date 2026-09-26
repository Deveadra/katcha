from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from katcha.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
engine = create_engine(settings.database_url, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

_model_metadata_loaded = False


def load_model_metadata() -> None:
    global _model_metadata_loaded
    if _model_metadata_loaded:
        return

    from katcha import (  # noqa: F401
        acquisition_models,
        brand_models,
        brand_preview_models,
        discovery_poll_models,
        discovery_trend_models,
        edit_blueprint_models,
        edit_performance_models,
        intelligence_models,
        longform_models,
        models,
        packaging_models,
        production_models,
        publishing_models,
        short_episode_models,
        trend_activation_models,
        trend_calibration_models,
        trend_models,
    )

    _model_metadata_loaded = True


@contextmanager
def session_scope() -> Iterator[Session]:
    load_model_metadata()
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db() -> None:
    load_model_metadata()
    Base.metadata.create_all(bind=engine)
