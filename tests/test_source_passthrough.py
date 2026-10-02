from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from katcha import db
from katcha.api.main import app
from katcha.intelligence_models import ChannelProfile
from katcha.models import Clip, SourceItem
from katcha.production_models import ProductionAsset
from katcha.publishing_models import YouTubeConnection
from katcha.services import productions


class FakeStore:
    def exists(self, key: str) -> bool:
        return bool(key)


@pytest.fixture()
def passthrough_scope(monkeypatch: pytest.MonkeyPatch):
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    db.load_model_metadata()
    db.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", factory)
    monkeypatch.setattr(productions, "ObjectStore", FakeStore)

    @contextmanager
    def scope() -> Iterator[Session]:
        session = factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    yield scope
    engine.dispose()


def _seed_forescene(scope) -> tuple[uuid.UUID, uuid.UUID]:
    connection_id = uuid.uuid4()
    profile_id = uuid.uuid4()
    clip_id = uuid.uuid4()
    with scope() as session:
        session.add(
            YouTubeConnection(
                id=connection_id,
                channel_id="UC" + "f" * 22,
                channel_title="FORESCENE",
                status="active",
                scopes=["https://www.googleapis.com/auth/youtube"],
                encrypted_access_token="encrypted-access",
                encrypted_refresh_token="encrypted-refresh",
                token_expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        session.add(
            ChannelProfile(
                id=profile_id,
                youtube_connection_id=connection_id,
                status="active",
                timezone="America/Chicago",
                profile_metadata={
                    "channel_title": "FORESCENE",
                    "channel_handle": "@watchforescene",
                },
            )
        )
        session.add(
            Clip(
                id=clip_id,
                sha256="a" * 64,
                storage_key="raw/a" + "a" * 63 + ".mp4",
                extension="mp4",
                size_bytes=1024,
                duration_seconds=Decimal("159.000"),
                width=1920,
                height=1080,
                status="ingested",
                media_metadata={},
            )
        )
        session.flush()
        session.add(
            SourceItem(
                source_url="https://www.youtube.com/watch?v=visionquest-final",
                canonical_url="https://www.youtube.com/watch?v=visionquest-final",
                platform="youtube",
                status="ready",
                title="Marvel Television's VisionQuest | Official Trailer",
                creator="Marvel Entertainment",
                clip_id=clip_id,
                source_metadata={
                    "channel_profile_id": str(profile_id),
                    "intelligence_title": "VisionQuest Final Trailer",
                    "intelligence_summary": "Official Marvel trailer retained for urgent release.",
                },
            )
        )
    return profile_id, clip_id


def test_source_passthrough_reuses_source_media_without_rendering(
    passthrough_scope,
) -> None:
    profile_id, clip_id = _seed_forescene(passthrough_scope)

    first = productions.register_source_passthrough_production(
        clip_id,
        channel_profile_id=profile_id,
        idempotency_key="visionquest-final",
        actor="orion",
    )
    replay = productions.register_source_passthrough_production(
        clip_id,
        channel_profile_id=profile_id,
        idempotency_key="visionquest-final",
        actor="orion",
    )

    assert replay.id == first.id
    assert first.kind == "source_passthrough"
    assert first.status == "approved"
    assert first.stage == "source_passthrough_approved"
    assert first.channel_profile_id == profile_id
    assert first.analysis_snapshot["source_passthrough"] is True
    assert "Marvel Entertainment" in " ".join(
        first.analysis_snapshot["grounding_facts"]
    )

    with passthrough_scope() as session:
        asset = session.scalar(
            select(ProductionAsset).where(
                ProductionAsset.production_id == first.id,
                ProductionAsset.kind == "render",
                ProductionAsset.generation == 1,
            )
        )
        assert asset is not None
        assert asset.storage_key.startswith("raw/")
        assert asset.provider == "source_passthrough"
        assert asset.asset_metadata["verified"] is True
        assert asset.asset_metadata["passthrough"] is True


def test_passthrough_route_is_mounted_and_channel_scoped() -> None:
    schema = app.openapi()
    path = schema["paths"]["/v1/clips/{clip_id}/passthrough-productions"]["post"]
    request_schema = path["requestBody"]["content"]["application/json"]["schema"]
    component_name = request_schema["$ref"].split("/")[-1]
    component = schema["components"]["schemas"][component_name]
    assert "channel_profile_id" in component["required"]
