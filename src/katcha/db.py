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


@contextmanager
def session_scope() -> Iterator[Session]:
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
    from katcha import (  # noqa: F401
        acquisition_models,
        brand_models,
        discovery_trend_models,
        intelligence_models,
        longform_models,
        models,
        production_models,
        publishing_models,
        short_episode_models,
        trend_calibration_models,
        trend_models,
    )

    Base.metadata.create_all(bind=engine)
