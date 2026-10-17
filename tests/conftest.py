"""Shared SQLite reference graph for media-deletion guard and stop tests."""
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from katcha import db
from katcha.intelligence_models import ChannelProfile
from katcha.longform_models import Compilation, CompilationSegment
from katcha.models import Clip
from katcha.production_models import Production
from katcha.short_episode_models import ShortEpisode, ShortEpisodeItem


@pytest.fixture
def fixture_work(monkeypatch):
    db.load_model_metadata()
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    db.Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=engine, expire_on_commit=False))
    clip_id, channel, other = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with db.session_scope() as session:
        session.add_all([
            ChannelProfile(id=channel, youtube_connection_id=uuid.uuid4(),
                           profile_metadata={"channel_title": "Forescene"}),
            ChannelProfile(id=other, youtube_connection_id=uuid.uuid4(),
                           profile_metadata={"channel_title": "RankSnaxx"}),
            Clip(id=clip_id, sha256="a" * 64, storage_key="raw/fixture.mp4"),
        ])
        production = Production(
            clip_id=clip_id, channel_profile_id=channel, workflow_id="purge-prod",
            status="review", stage="review", persona_key="fixture",
            persona_version="1", prompt_version="1",
        )
        old = Production(
            clip_id=clip_id, channel_profile_id=channel, workflow_id="purge-old",
            status="approved", stage="approved", persona_key="fixture",
            persona_version="1", prompt_version="1",
        )
        episode = ShortEpisode(
            channel_profile_id=channel, workflow_id="purge-episode",
            status="planned", stage="planned", premise="Ranked highlights",
            format_key="countdown", format_version="1", item_count=1,
            persona_key="fixture", persona_version="1",
            brand_key="fixture", brand_version=1,
        )
        compilation = Compilation(
            channel_profile_id=other, workflow_id="purge-compilation",
            status="rendering", stage="rendering", theme="Trailers this week",
            target_duration_seconds=600,
        )
        session.add_all([production, old, episode, compilation])
        session.flush()
        ids = {"production": production.id, "old": old.id,
               "short_episode": episode.id, "compilation": compilation.id}
        session.add_all([
            ShortEpisodeItem(
                short_episode_id=episode.id, clip_id=clip_id, position=1,
                role="hook", overall_score=Decimal("0.9"),
                role_score=Decimal("0.8"),
            ),
            CompilationSegment(
                compilation_id=compilation.id, clip_id=clip_id, position=1,
                deterministic_score=Decimal("0.9"),
                opening_score=Decimal("0.8"),
                source_duration_seconds=Decimal("10"),
            ),
        ])
    yield clip_id, channel, other, ids
    engine.dispose()


