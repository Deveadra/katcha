from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from katcha.ai.router import ModelRoute, ModelTarget
from katcha.ai.schemas import (
    PackagingCandidate,
    PackagingCandidateSet,
    PackagingThumbnailBrief,
)
from katcha.api.main import app
from katcha.db import Base
from katcha.packaging_models import PackagingCandidateGeneration
from katcha.publishing_models import Publication, YouTubeConnection
from katcha.services import packaging as packaging_service
from katcha.services import packaging_generation


@pytest.fixture
def generation_scope(monkeypatch: pytest.MonkeyPatch):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def scope():
        session: Session = factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    monkeypatch.setattr(packaging_generation, "session_scope", scope)
    monkeypatch.setattr(packaging_service, "session_scope", scope)
    return scope


def _publication(scope) -> Publication:
    with scope() as session:
        connection = YouTubeConnection(
            channel_id="channel-generation",
            channel_title="Generation Channel",
            status="active",
            scopes=["youtube"],
            encrypted_access_token="encrypted",
            encrypted_refresh_token="encrypted",
            token_expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        session.add(connection)
        session.flush()
        publication = Publication(
            production_id=uuid.uuid4(),
            youtube_connection_id=connection.id,
            workflow_id="publication-generation",
            analytics_workflow_id="analytics-generation",
            title="Original title",
            description="Original description",
            youtube_video_id="video-generation",
        )
        session.add(publication)
        session.flush()
        session.refresh(publication)
        session.expunge(publication)
        return publication


def _candidate(index: int) -> PackagingCandidate:
    return PackagingCandidate(
        variation_family=f"family-{index}",
        angle=f"Angle {index}",
        title=f"Cat jump angle {index}",
        description=f"A factual description for angle {index}.",
        supporting_facts=["A cat jumps over the couch."],
        thumbnail=PackagingThumbnailBrief(
            concept=f"Freeze the jump at angle {index}",
            focal_subject="The cat in mid-air",
            composition="Tight crop around the cat and couch",
            on_image_text=None,
            emotion="surprise",
            avoid=["invented people"],
        ),
    )


def test_candidate_validator_rejects_unsupported_grounding_fact() -> None:
    candidate = _candidate(1)
    candidate.supporting_facts = ["A dog appears."]
    with pytest.raises(ValueError, match="unsupported grounding fact"):
        packaging_generation._validate_candidates(
            PackagingCandidateSet(candidates=[candidate, _candidate(2)]),
            candidate_count=2,
            context={
                "current_title": "Original",
                "existing_variants": [],
                "grounding_facts": ["A cat jumps over the couch."],
            },
        )


def test_generation_key_replay_does_not_call_provider_twice(
    generation_scope,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication = _publication(generation_scope)
    profile_id = uuid.uuid4()
    context = {
        "current_title": "Original title",
        "existing_variants": [],
        "grounding_facts": ["A cat jumps over the couch."],
        "brand_key": "test-brand",
        "brand_version": 1,
        "edit_blueprint_key": "persona_commentary",
        "edit_blueprint_version": 1,
    }
    monkeypatch.setattr(
        packaging_generation,
        "compile_packaging_context",
        lambda _publication_id: (profile_id, context, "a" * 64),
    )
    monkeypatch.setattr(packaging_generation, "assert_ai_budget", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        packaging_generation,
        "route_for_channel",
        lambda *_args, **_kwargs: SimpleNamespace(
            route=ModelRoute(
                primary=ModelTarget("openai", "gpt-5.6-luna"),
                fallback=None,
            ),
            reservation_id=uuid.uuid4(),
        ),
    )
    calls = {"count": 0}

    def fake_generate(*_args, **_kwargs):
        calls["count"] += 1
        return PackagingCandidateSet(
            candidates=[_candidate(1), _candidate(2), _candidate(3)]
        )

    monkeypatch.setattr(packaging_generation, "_openai_generate", fake_generate)

    first = packaging_generation.generate_packaging_candidates(
        publication.id,
        generation_key="auto-v1",
        candidate_count=3,
    )
    second = packaging_generation.generate_packaging_candidates(
        publication.id,
        generation_key="auto-v1",
        candidate_count=3,
    )

    assert calls["count"] == 1
    assert first.generation.status == "completed"
    assert second.generation.id == first.generation.id
    assert [row.id for row in second.variants] == [row.id for row in first.variants]
    assert len(first.variants) == 3
    assert all(row.created_by == "katcha-ai" for row in first.variants)
    assert first.variants[0].variant_metadata["thumbnail_brief"]["concept"]


def test_generation_model_has_publication_key_idempotency() -> None:
    uniques = {
        tuple(column.name for column in constraint.columns)
        for constraint in PackagingCandidateGeneration.__table__.constraints
        if hasattr(constraint, "columns")
    }
    assert ("publication_id", "generation_key") in uniques


def test_packaging_generation_routes_are_mounted() -> None:
    paths = set(app.openapi()["paths"])
    assert "/v1/publications/{publication_id}/packaging/generations" in paths
