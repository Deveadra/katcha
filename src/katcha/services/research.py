"""Connect saved Sources and channel interests to the durable discovery pipeline."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from katcha.acquisition_models import DiscoveryRun, IngestionSource, TopicWatchVersion
from katcha.config import get_settings
from katcha.db import session_scope
from katcha.domain import SourceUsageMode
from katcha.intelligence_models import ChannelProfile
from katcha.models import DomainEvent
from katcha.services.discovery_trends import create_topic_watch_version, latest_topic_watch
from katcha.services.trends import active_watch_profile

PULL_ADAPTERS = frozenset({"youtube", "reddit", "rss_atom", "web_scout"})
RESEARCH_WORKFLOW_ID = "katcha-automatic-research"


def source_research_enabled(source: IngestionSource) -> bool:
    return bool(
        source.enabled
        and source.usage_mode != SourceUsageMode.BLOCKED.value
        and source.adapter_key in PULL_ADAPTERS
        and (source.source_metadata or {}).get("automatic_research", True)
    )


def _watch_job(
    *,
    key: str,
    channel_id: uuid.UUID | None,
    spec: dict,
    interval: int,
    now: datetime,
) -> dict[str, object] | None:
    fingerprint = hashlib.sha256(json.dumps(spec, sort_keys=True, default=str).encode()).hexdigest()
    with session_scope() as session:
        current = latest_topic_watch(session, key, channel_profile_id=channel_id)
        if current is not None:
            session.expunge(current)
    if current is None or (current.watch_metadata or {}).get("research_fingerprint") != fingerprint:
        current = create_topic_watch_version(
            watch_key=key,
            channel_profile_id=channel_id,
            **spec,
            metadata={"automatic_research": True, "research_fingerprint": fingerprint},
        )
    bucket = int(now.timestamp()) // (max(interval, 1) * 60)
    execution_key = f"auto-{bucket}"
    with session_scope() as session:
        # The child workflow owns any in-flight run; restarting the scheduler must
        # not launch a second provider request for the same collection window.
        existing = session.scalar(
            select(DiscoveryRun.id)
            .where(DiscoveryRun.run_key.like(f"watch:{current.id}:{execution_key}:%"))
            .limit(1)
        )
    if existing is not None:
        return None
    return {
        "topic_watch_id": str(current.id),
        "execution_key": execution_key,
        "workflow_id": f"auto-research-{current.id}-{bucket}",
        "top_n": min(current.max_candidates, 50),
    }


def prepare_research_jobs(now: datetime | None = None) -> list[dict[str, object]]:
    now = now or datetime.now(UTC)
    settings = get_settings()
    if not settings.research_enabled:
        return []
    with session_scope() as session:
        sources = list(session.scalars(select(IngestionSource).order_by(IngestionSource.id)))
        profiles = list(
            session.scalars(
                select(ChannelProfile)
                .where(ChannelProfile.status == "active")
                .order_by(ChannelProfile.id)
            )
        )
        for row in [*sources, *profiles]:
            session.expunge(row)
    active_channels = {profile.id for profile in profiles}
    jobs: list[dict[str, object]] = []
    specifications: list[dict] = []
    for source in sources:
        if not source_research_enabled(source):
            continue
        if source.channel_profile_id and source.channel_profile_id not in active_channels:
            continue
        try:
            query = dict(source.query_template or {})
            specifications.append(
                {
                    "key": f"source-{source.id}",
                    "channel_id": source.channel_profile_id,
                    "interval": source.poll_interval_minutes,
                    "spec": {
                        "name": source.name,
                        "include_terms": query.get("include_terms") or [],
                        "exclude_terms": query.get("exclude_terms") or [],
                        "freshness_horizon_hours": int(query.get("freshness_horizon_hours") or 72),
                        "max_candidates": min(max(int(query.get("limit") or 40), 1), 100),
                        "adapter_configs": [
                            {
                                "adapter_key": source.adapter_key,
                                "adapter_version": source.adapter_version,
                                "ingestion_source_id": str(source.id),
                                "query": query,
                            }
                        ],
                    },
                }
            )
        except (ValueError, TypeError):
            logging.getLogger(__name__).exception("Could not prepare source %s", source.id)
    for profile in profiles:
        interests = active_watch_profile(profile.id)
        if interests is None or not (interests.watch_metadata or {}).get(
            "automatic_research", True
        ):
            continue
        platforms = list(interests.platforms or [])
        configs = []
        # Native YouTube search reuses the channel's OAuth connection. It does
        # not require a second AI subscription or a separate Data API key.
        if not platforms or "youtube" in platforms:
            configs.append(
                {
                    "adapter_key": "youtube",
                    "adapter_version": "v1",
                    "query": {"youtube_connection_id": str(profile.youtube_connection_id)},
                }
            )
        if settings.ai_enabled and settings.resolved_ai_execution_mode() == "live":
            from katcha.integrations.chatgpt import connection_status as chatgpt_status
            from katcha.integrations.codex import connection_status as codex_status

            web_ready = (
                bool(settings.openai_api_key)
                or (settings.codex_enabled and codex_status().get("connected"))
                or (settings.chatgpt_host_id and chatgpt_status().get("plan_usage_enabled"))
            )
            if web_ready:
                configs.append(
                    {
                        "adapter_key": "web_scout",
                        "adapter_version": "v1",
                        "query": {
                            "operator_request": (
                                "Find current videos and stories for these interests."
                            ),
                            "platforms": platforms,
                        },
                    }
                )
        if configs:
            specifications.append(
                {
                    "key": "channel-interest-research",
                    "channel_id": profile.id,
                    "interval": 60,
                    "spec": {
                        "name": "Channel interest research",
                        "include_terms": list(interests.interests),
                        "exclude_terms": list(interests.excluded_terms or []),
                        "language": next(iter(interests.languages or []), None),
                        "locale": next(iter(interests.regions or []), None),
                        "freshness_horizon_hours": interests.freshness_horizon_hours,
                        "max_candidates": 40,
                        "adapter_configs": configs,
                    },
                }
            )
    for spec in specifications:
        try:
            job = _watch_job(**spec, now=now)
            if job:
                jobs.append(job)
        except (ValueError, TypeError):
            # One malformed legacy source must not stop every other source.
            logging.getLogger(__name__).exception("Could not prepare research %s", spec["key"])
    with session_scope() as session:
        session.add(
            DomainEvent(
                aggregate_type="research_scheduler",
                aggregate_id=RESEARCH_WORKFLOW_ID,
                event_type="research.scheduler_tick",
                payload={"due_jobs": len(jobs), "configured_jobs": len(specifications)},
            )
        )
    return jobs


def research_watch_is_current(watch: TopicWatchVersion) -> bool:
    if not (watch.watch_metadata or {}).get("automatic_research"):
        return True
    if not get_settings().research_enabled:
        return False
    with session_scope() as session:
        latest = latest_topic_watch(
            session,
            watch.watch_key,
            channel_profile_id=watch.channel_profile_id,
        )
        if latest is None or latest.id != watch.id:
            return False
        if watch.channel_profile_id:
            profile = session.get(ChannelProfile, watch.channel_profile_id)
            if profile is None or profile.status != "active":
                return False
    if watch.watch_key == "channel-interest-research" and watch.channel_profile_id:
        interests = active_watch_profile(watch.channel_profile_id)
        if interests is None or not (interests.watch_metadata or {}).get(
            "automatic_research", True
        ):
            return False
    return True
