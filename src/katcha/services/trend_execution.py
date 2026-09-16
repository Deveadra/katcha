from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from katcha.acquisition.adapters import get_adapter
from katcha.acquisition_models import DiscoveryRun, TopicWatchVersion
from katcha.db import session_scope
from katcha.services.acquisition import register_discovery_run
from katcha.services.trend_source_reliability import (
    ensure_topic_watch_source_states,
    source_state_snapshot,
)

_SECRET_FRAGMENTS = (
    "authorization",
    "credential",
    "password",
    "secret",
    "token",
    "api_key",
)


@dataclass(frozen=True, slots=True)
class PreparedDiscoveryRun:
    run_id: uuid.UUID
    adapter_key: str
    adapter_version: str
    workflow_id: str


def contains_secret_key(value: object) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).casefold().replace("-", "_")
            if any(fragment in normalized for fragment in _SECRET_FRAGMENTS):
                return True
            if contains_secret_key(child):
                return True
    elif isinstance(value, list):
        return any(contains_secret_key(item) for item in value)
    return False


def normalize_adapter_config(
    raw: dict[str, Any],
) -> tuple[str, str, dict[str, Any]]:
    if contains_secret_key(raw):
        raise ValueError(
            "topic watch adapter configuration must not contain credentials or tokens"
        )
    adapter_key = str(raw.get("adapter_key") or "").strip()
    adapter_version = str(raw.get("adapter_version") or "v1").strip()
    query = raw.get("query", {})
    if not adapter_key:
        raise ValueError("topic watch adapter config is missing adapter_key")
    if not isinstance(query, dict):
        raise ValueError("topic watch adapter config query must be an object")
    get_adapter(adapter_key, adapter_version)
    return adapter_key, adapter_version, dict(query)


def prepare_topic_watch_execution(
    topic_watch_id: uuid.UUID,
    *,
    execution_key: str,
) -> list[PreparedDiscoveryRun]:
    key = execution_key.strip()
    if not key:
        raise ValueError("execution_key is required")
    if len(key) > 80:
        raise ValueError("execution_key must be 80 characters or fewer")

    ensure_topic_watch_source_states(topic_watch_id)
    with session_scope() as session:
        watch = session.get(TopicWatchVersion, topic_watch_id)
        if watch is None:
            raise ValueError(f"topic watch version not found: {topic_watch_id}")
        if not watch.enabled:
            raise ValueError("topic watch is disabled")
        watch_key = watch.watch_key
        watch_version = watch.version
        channel_profile_id = watch.channel_profile_id
        include_terms = list(watch.include_terms or [])
        exclude_terms = list(watch.exclude_terms or [])
        freshness_horizon_hours = watch.freshness_horizon_hours
        max_candidates = watch.max_candidates
        language = watch.language
        locale = watch.locale
        configs = [dict(item) for item in (watch.adapter_configs or [])]

    if not configs:
        raise ValueError("topic watch has no adapters configured")

    runs: list[PreparedDiscoveryRun] = []
    for index, config in enumerate(configs):
        state = source_state_snapshot(topic_watch_id, index)
        if bool(state.get("backoff_active")):
            continue
        initial_cursor = dict(state.get("cursor") or {})
        adapter_key, adapter_version, query = normalize_adapter_config(config)
        query["include_terms"] = include_terms
        query["exclude_terms"] = exclude_terms
        query["freshness_horizon_hours"] = freshness_horizon_hours
        requested_limit = int(query.get("limit", max_candidates))
        query["limit"] = max(1, min(requested_limit, max_candidates))
        if language and adapter_key == "youtube":
            query.setdefault("relevance_language", language)
        if locale and adapter_key == "youtube":
            region = locale.split("-")[-1].strip().upper()
            if len(region) == 2:
                query.setdefault("region_code", region)

        run_key = f"watch:{topic_watch_id}:{key}:{index}"
        run = register_discovery_run(
            adapter_key=adapter_key,
            adapter_version=adapter_version,
            query=query,
            idempotency_key=run_key,
            metadata={
                "topic_watch_id": str(topic_watch_id),
                "channel_profile_id": (
                    str(channel_profile_id) if channel_profile_id else None
                ),
                "watch_key": watch_key,
                "watch_version": watch_version,
                "execution_key": key,
                "adapter_index": index,
            },
        )
        if initial_cursor:
            with session_scope() as session:
                stored = session.get(DiscoveryRun, run.id)
                if stored is not None and not stored.cursor:
                    stored.cursor = initial_cursor
        runs.append(
            PreparedDiscoveryRun(
                run_id=run.id,
                adapter_key=adapter_key,
                adapter_version=adapter_version,
                workflow_id=f"discovery-run-{run.id}",
            )
        )
    return runs
