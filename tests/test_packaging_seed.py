from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import katcha.api.main  # noqa: F401  # Populate the shared model registry.
import katcha.db as db
import katcha.services.packaging_seed as seed
from katcha.intelligence_models import ChannelProfile
from katcha.packaging_models import PublicationPackagingActivation, PublicationPackagingVariant
from katcha.publishing_models import Publication, YouTubeConnection
from katcha.services.packaging_baseline import record_initial_packaging


@pytest.fixture
def publication(monkeypatch):
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    db.Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", factory)
    channel_id, publication_id = uuid.uuid4(), uuid.uuid4()
    now = datetime.now(UTC)
    with factory.begin() as session:
        connection = YouTubeConnection(
            channel_id="fixture",
            channel_title="Fixture",
            status="active",
            scopes=[],
            encrypted_access_token="fixture",
            encrypted_refresh_token="fixture",
            token_expires_at=now + timedelta(hours=1),
        )
        session.add(connection)
        session.flush()
        session.add(
            ChannelProfile(id=channel_id, youtube_connection_id=connection.id, status="active")
        )
        session.add(
            Publication(
                id=publication_id,
                production_id=uuid.uuid4(),
                youtube_connection_id=connection.id,
                workflow_id="seed-fixture",
                analytics_workflow_id="seed-analytics",
                status="published",
                youtube_video_id="fixture-video",
                title="Initial title",
                description="Initial description",
                published_at=now - timedelta(days=1),
                treatment_metadata={},
            )
        )
    yield SimpleNamespace(factory=factory, channel=channel_id, publication=publication_id)
    engine.dispose()


def test_record_existing_package_without_provider_mutation(publication):
    first = record_initial_packaging(publication.publication)
    assert first == record_initial_packaging(publication.publication)
    with publication.factory() as session:
        variant = session.get(PublicationPackagingVariant, first)
        assert variant.title == "Initial title"
        assert variant.thumbnail_storage_key is None
        rows = list(
            session.scalars(
                select(PublicationPackagingActivation).where(
                    PublicationPackagingActivation.publication_id == publication.publication,
                )
            )
        )
        assert len(rows) == 1
        assert rows[0].status == "applied"
        assert rows[0].activation_metadata["no_provider_mutation"] is True


def test_backfill_preserves_existing_packaging_activation(publication):
    existing_id = uuid.uuid4()
    with publication.factory.begin() as session:
        session.add(
            PublicationPackagingVariant(
                id=existing_id,
                publication_id=publication.publication,
                variant_key="operator",
                version=1,
                title="Already applied",
                description="Existing",
                variant_metadata={},
            )
        )
        session.add(
            PublicationPackagingActivation(
                publication_id=publication.publication,
                variant_id=existing_id,
                activation_key="operator-activation",
                workflow_id="operator-workflow",
                status="applied",
                stage="completed",
                youtube_video_id="fixture-video",
                applied_at=datetime.now(UTC),
            )
        )
    assert record_initial_packaging(publication.publication) == existing_id
    with publication.factory() as session:
        assert len(list(session.scalars(select(PublicationPackagingVariant)))) == 1


def test_seed_requires_ai_and_is_channel_scoped(publication, monkeypatch):
    monkeypatch.setattr(
        seed,
        "get_settings",
        lambda: SimpleNamespace(ai_enabled=False, openai_api_key=None, gemini_api_key=None),
    )
    result = seed.seed_channel_packaging(publication.channel)
    assert result["generated"] == []
    assert result["blocked"][0]["reason"] == "ai_disabled"
    with publication.factory() as session:
        assert session.scalar(select(PublicationPackagingActivation)).status == "applied"
    assert seed.seed_channel_packaging(uuid.uuid4())["reason"] == "channel_inactive"


def test_seed_uses_stable_generation_key_without_provider_retry(publication, monkeypatch):
    monkeypatch.setattr(
        seed,
        "get_settings",
        lambda: SimpleNamespace(ai_enabled=True, openai_api_key="test-only", gemini_api_key=None),
    )
    calls = []

    def fake_generate(publication_id, *, generation_key, candidate_count):
        calls.append((publication_id, generation_key, candidate_count))
        return SimpleNamespace(generation=SimpleNamespace(id=uuid.uuid4()), variants=())

    monkeypatch.setattr(seed, "generate_packaging_candidates", fake_generate)
    result = seed.seed_channel_packaging(publication.channel)
    assert len(result["generated"]) == 1
    assert calls == [(publication.publication, "auto-initial-v1", 3)]
    with publication.factory.begin() as session:
        pub = session.get(Publication, publication.publication)
        pub.status = "failed"
    assert seed.seed_channel_packaging(publication.channel)["generated"] == []
