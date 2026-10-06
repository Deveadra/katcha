import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from katcha.db import session_scope
from katcha.domain import ChannelStatus
from katcha.editorial_models import EditorialProject, EditorialRevision, EditorialRun
from katcha.intelligence_models import ChannelProfile
from katcha.models import Clip
from katcha.production_models import Production
from katcha.publishing_models import Publication, YouTubeConnection
from katcha.services.command_resources import (
    resolve_command_resources,
    resource_context_summary,
)
from katcha.short_episode_models import ShortEpisode
from katcha.trend_models import TrendOpportunity, TrendTopic


def _channel(title: str) -> tuple[uuid.UUID, uuid.UUID]:
    connection_id = uuid.uuid4()
    profile_id = uuid.uuid4()
    now = datetime.now(UTC)
    with session_scope() as session:
        session.add(
            YouTubeConnection(
                id=connection_id,
                channel_id=f"fixture-{connection_id}",
                channel_title=title,
                status="active",
                scopes=[],
                encrypted_access_token="fixture",
                encrypted_refresh_token="fixture",
                token_expires_at=now + timedelta(hours=1),
                connection_metadata={},
            )
        )
        session.flush()
        session.add(
            ChannelProfile(
                id=profile_id,
                youtube_connection_id=connection_id,
                status=ChannelStatus.ACTIVE.value,
                timezone="America/Chicago",
                active_strategy_version=1,
                active_automation_version=1,
                profile_metadata={"channel_title": title},
            )
        )
    return profile_id, connection_id


def test_typed_resources_are_channel_scoped_and_grounded() -> None:
    profile_id, connection_id = _channel("Resource Fixture")
    other_profile_id, _ = _channel("Other Fixture")
    clip_id = uuid.uuid4()
    production_id = uuid.uuid4()
    episode_id = uuid.uuid4()
    publication_id = uuid.uuid4()
    topic_id = uuid.uuid4()
    opportunity_id = uuid.uuid4()

    with session_scope() as session:
        session.add(
            Clip(
                id=clip_id,
                sha256=uuid.uuid4().hex * 2,
                storage_key=f"clips/{clip_id}.mp4",
                extension="mp4",
                status="analyzed",
                media_metadata={},
            )
        )
        session.flush()
        session.add(
            Production(
                id=production_id,
                clip_id=clip_id,
                channel_profile_id=profile_id,
                workflow_id=f"production-{production_id}",
                status="failed",
                stage="render",
                persona_key="fixture",
                persona_version="v1",
                prompt_version="v1",
                estimated_cost_usd=Decimal("0.12"),
                error="renderer fixture failure",
            )
        )
        session.add(
            ShortEpisode(
                id=episode_id,
                channel_profile_id=profile_id,
                workflow_id=f"episode-{episode_id}",
                status="planned",
                stage="planned",
                premise="Fixture ranked episode",
                format_key="ranked",
                format_version="v1",
                item_count=1,
                persona_key="fixture",
                persona_version="v1",
                brand_key="fixture",
                brand_version=1,
            )
        )
        session.add(
            TrendTopic(
                id=topic_id,
                topic_key=f"topic-{topic_id}",
                display_name="Fixture Opportunity",
                aliases=[],
                tags=["gaming"],
                first_seen_at=datetime.now(UTC),
                last_seen_at=datetime.now(UTC),
                topic_metadata={},
            )
        )
        session.flush()
        session.add(
            TrendOpportunity(
                id=opportunity_id,
                channel_profile_id=profile_id,
                trend_topic_id=topic_id,
                watch_version=1,
                run_key=f"run-{opportunity_id}",
                lifecycle="emerging",
                opportunity_score=Decimal("0.82"),
                confidence=Decimal("0.91"),
                rank=1,
                expires_at=datetime.now(UTC) + timedelta(hours=24),
                reasons=["velocity"],
                evidence_summary={"source_count": 4},
            )
        )
        session.add(
            Publication(
                id=publication_id,
                production_id=production_id,
                youtube_connection_id=connection_id,
                workflow_id=f"publish-{publication_id}",
                analytics_workflow_id=f"analytics-{publication_id}",
                status="published",
                stage="published",
                title="Fixture publication",
                privacy_status="private",
            )
        )

    evidence = resolve_command_resources(
        profile_id,
        [
            ("clip", clip_id),
            ("production", production_id),
            ("short_episode", episode_id),
            ("publication", publication_id),
            ("trend_opportunity", opportunity_id),
        ],
    )

    assert [item["kind"] for item in evidence] == [
        "clip",
        "production",
        "short_episode",
        "publication",
        "trend_opportunity",
    ]
    assert evidence[1]["error"] == "renderer fixture failure"
    assert evidence[3]["title"] == "Fixture publication"
    assert evidence[4]["topic"] == "Fixture Opportunity"
    assert "Fixture Opportunity" in resource_context_summary(evidence)

    with pytest.raises(ValueError, match="different channel|not available"):
        resolve_command_resources(other_profile_id, [("production", production_id)])


def test_editorial_project_context_is_channel_scoped_and_bounded() -> None:
    profile_id, _ = _channel("Editorial Context")
    other_profile_id, _ = _channel("Other Editorial Context")
    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    source_urls = [
        "https://example.com/source",
        "https://upload.katcha.invalid/local",
    ]
    with session_scope() as session:
        session.add(
            EditorialProject(
                id=project_id,
                channel_profile_id=profile_id,
                input_digest="a" * 64,
                brief={
                    "prompt": "Investigate the trailer clues",
                    "target_seconds": 420,
                    "source_urls": source_urls,
                    "source_clip_bindings": {
                        source_urls[0]: str(uuid.uuid4()),
                        source_urls[1]: str(uuid.uuid4()),
                    },
                    "script_seed": {
                        "text": "This raw seed must not enter command evidence.",
                        "origin": "operator_file",
                        "filename": "draft.md",
                        "media_type": "text/markdown",
                        "content_sha256": "b" * 64,
                    },
                },
                revision=2,
            )
        )
        session.add(
            EditorialRevision(
                project_id=project_id,
                revision=2,
                request_id=uuid.uuid4(),
                request_digest="c" * 64,
                digest="d" * 64,
                draft={
                    "script": [
                        {
                            "beat_id": "one",
                            "role": "reveal",
                            "narration": "First grounded beat.",
                            "visual_intent": "Show the exact clue.",
                            "planned_duration_seconds": 8,
                            "claim_ids": [],
                        },
                        {"beat_id": "two", "narration": "Second grounded beat here."},
                    ]
                },
                actor="control-principal:editor",
            )
        )
        session.add(
            EditorialRun(
                id=run_id,
                project_id=project_id,
                channel_profile_id=profile_id,
                input_digest="e" * 64,
                input_revision=2,
                options={"target": "direction"},
                status="completed",
                stage="direction_ready",
                artifacts={},
                actor="control-principal:editor",
            )
        )

    evidence = resolve_command_resources(
        profile_id,
        [("editorial_project", project_id)],
    )

    assert len(evidence) == 1
    item = evidence[0]
    assert item["kind"] == "editorial_project"
    assert item["title"] == "Investigate the trailer clues"
    assert item["source_urls"] == source_urls
    assert item["source_count"] == 2
    assert item["managed_clip_count"] == 2
    assert item["script_seed"]["filename"] == "draft.md"
    assert "text" not in item["script_seed"]
    assert item["latest_revision"]["revision"] == 2
    assert item["latest_revision"]["script_beat_count"] == 2
    assert item["latest_revision"]["script_word_count"] == 7
    assert item["latest_run"]["id"] == str(run_id)
    assert item["latest_run"]["stage"] == "direction_ready"
    assert item["latest_run"]["target"] == "direction"
    assert "Investigate the trailer clues" in resource_context_summary(evidence)

    selected = resolve_command_resources(
        profile_id,
        [("editorial_project", project_id, "one")],
    )[0]
    assert selected["selected_beat"]["id"] == "one"
    assert selected["selected_beat"]["revision"] == 2
    assert selected["selected_beat"]["narration"] == "First grounded beat."
    assert selected["selected_beat"]["visual_intent"] == "Show the exact clue."

    with pytest.raises(ValueError, match="editorial beat not found"):
        resolve_command_resources(
            profile_id,
            [("editorial_project", project_id, "missing")],
        )

    with pytest.raises(ValueError, match="different channel"):
        resolve_command_resources(
            other_profile_id,
            [("editorial_project", project_id)],
        )


def test_typed_resource_context_deduplicates_and_caps_input() -> None:
    profile_id, _ = _channel("Resource Limits")
    clip_id = uuid.uuid4()
    production_id = uuid.uuid4()
    with session_scope() as session:
        session.add(
            Clip(
                id=clip_id,
                sha256=uuid.uuid4().hex * 2,
                storage_key=f"clips/{clip_id}.mp4",
                extension="mp4",
                status="analyzed",
                media_metadata={},
            )
        )
        session.flush()
        session.add(
            Production(
                id=production_id,
                clip_id=clip_id,
                channel_profile_id=profile_id,
                workflow_id=f"production-{production_id}",
                persona_key="fixture",
                persona_version="v1",
                prompt_version="v1",
            )
        )

    rows = resolve_command_resources(
        profile_id,
        [("clip", clip_id), ("clip", clip_id)],
    )
    assert len(rows) == 1

    with pytest.raises(ValueError, match="at most 8"):
        resolve_command_resources(
            profile_id,
            [("clip", clip_id)] * 9,
        )
