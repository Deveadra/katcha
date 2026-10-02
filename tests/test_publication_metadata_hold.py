from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from katcha import db
from katcha.intelligence_models import ChannelProfile
from katcha.models import Clip
from katcha.production_models import Production, ProductionAsset
from katcha.publishing_models import YouTubeConnection
from katcha.services import publications


@pytest.fixture()
def publication_scope(monkeypatch: pytest.MonkeyPatch):
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    db.load_model_metadata()
    db.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", factory)
    monkeypatch.setattr(publications, "session_scope", lambda: _scope(factory))

    yield factory
    engine.dispose()


@contextmanager
def _scope(factory) -> Iterator[Session]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _seed(factory) -> tuple[uuid.UUID, uuid.UUID]:
    with _scope(factory) as session:
        connection = YouTubeConnection(
            channel_id="UC" + "p" * 22,
            channel_title="FORESCENE",
            status="active",
            scopes=["https://www.googleapis.com/auth/youtube"],
            encrypted_access_token="encrypted",
            encrypted_refresh_token="encrypted",
            token_expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        session.add(connection)
        session.flush()
        profile = ChannelProfile(
            youtube_connection_id=connection.id,
            status="active",
            timezone="America/Chicago",
            profile_metadata={"channel_title": "FORESCENE"},
        )
        session.add(profile)
        clip = Clip(
            sha256="b" * 64,
            storage_key="raw/" + "b" * 64 + ".mp4",
            extension="mp4",
            duration_seconds=Decimal("120.0"),
            status="ingested",
            media_metadata={},
        )
        session.add(clip)
        session.flush()
        production = Production(
            clip_id=clip.id,
            channel_profile_id=profile.id,
            workflow_id=f"passthrough-{uuid.uuid4()}",
            kind="source_passthrough",
            status="approved",
            stage="source_passthrough_approved",
            persona_key="youth_host",
            persona_version="1",
            prompt_version="source-passthrough-v1",
            brand_key="forescene",
            brand_version=1,
            brand_snapshot={},
            analysis_snapshot={"source_passthrough": True},
            render_manifest={"version": "source-passthrough-v1"},
            estimated_cost_usd=Decimal("0"),
        )
        session.add(production)
        session.flush()
        session.add(
            ProductionAsset(
                production_id=production.id,
                kind="render",
                generation=1,
                storage_key=clip.storage_key,
                content_type="video/mp4",
                provider="source_passthrough",
                model="original_source",
                asset_metadata={"verified": True},
            )
        )
        return production.id, connection.id


def test_publication_can_hold_for_seo_before_upload(publication_scope) -> None:
    production_id, connection_id = _seed(publication_scope)
    row = publications.register_publication(
        production_id,
        youtube_connection_id=connection_id,
        title="VisionQuest trailer metadata pending",
        description="",
        tags=[],
        privacy_status="public",
        hold_for_packaging=True,
    )

    assert row.status == "queued"
    assert row.stage == "metadata_hold"
    assert row.youtube_video_id is None

    released = publications.release_publication_for_upload(
        row.id,
        actor="orion",
    )
    assert released.status == "queued"
    assert released.stage == "queued"

    replay = publications.release_publication_for_upload(
        row.id,
        actor="orion",
    )
    assert replay.id == row.id
    assert replay.stage == "queued"
