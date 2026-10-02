from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from katcha import db
from katcha.api.main import app
from katcha.intelligence_models import ChannelProfile
from katcha.models import Clip, SourceItem
from katcha.production_models import Production, ProductionAsset
from katcha.publishing_models import Publication, YouTubeConnection
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
                    "intelligence_record_id": "00000000-0000-0000-0000-000000000123",
                    "discovery_metadata": {
                        "operator_authorized": True,
                        "authorization_scope": "official_trailer_repost",
                        "official_source_verified": True,
                        "intelligence_record_id": "00000000-0000-0000-0000-000000000123",
                        "intelligence_tags": ["visionquest", "official_trailer"],
                        "intelligence_payload": {
                            "production_intent": "source_passthrough",
                            "preupload_packaging_required": True,
                            "privacy_status": "public",
                        },
                    },
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


def test_post_ingest_activity_creates_visible_held_publication(
    passthrough_scope,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from katcha.orchestration.activities import prepare_authorized_passthrough_activity
    from katcha.services import trailer_passthrough

    profile_id, clip_id = _seed_forescene(passthrough_scope)
    with passthrough_scope() as session:
        source = session.scalar(select(SourceItem).where(SourceItem.clip_id == clip_id))
        assert source is not None
        source_id = source.id

    generation_id = uuid.uuid4()

    def fake_generate(publication_id, *, generation_key, candidate_count):
        return SimpleNamespace(
            generation=SimpleNamespace(
                id=generation_id,
                status="completed",
            ),
            variants=(),
        )

    monkeypatch.setattr(
        trailer_passthrough,
        "generate_packaging_candidates",
        fake_generate,
    )

    result = prepare_authorized_passthrough_activity(str(source_id))

    assert result["action"] == "prepared"
    assert result["clip_id"] == str(clip_id)
    assert result["packaging_status"] == "completed"
    assert result["packaging_generation_id"] == str(generation_id)

    with passthrough_scope() as session:
        publication = session.get(Publication, uuid.UUID(str(result["publication_id"])))
        assert publication is not None
        assert publication.stage == "metadata_hold"
        assert publication.status == "queued"
        assert publication.privacy_status == "public"
        assert publication.publish_at is None
        assert publication.title == "VisionQuest Final Trailer"
        production = session.get(Production, publication.production_id)
        assert production is not None
        assert production.channel_profile_id == profile_id
        assert production.kind == "source_passthrough"


def test_passthrough_route_is_mounted_and_channel_scoped() -> None:
    schema = app.openapi()
    path = schema["paths"]["/v1/clips/{clip_id}/passthrough-productions"]["post"]
    request_schema = path["requestBody"]["content"]["application/json"]["schema"]
    component_name = request_schema["$ref"].split("/")[-1]
    component = schema["components"]["schemas"][component_name]
    assert "channel_profile_id" in component["required"]
