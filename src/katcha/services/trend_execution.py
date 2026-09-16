from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from katcha.acquisition.adapters import get_adapter
from katcha.acquisition_models import DiscoveryRun, TopicWatchVersion
from katcha.db import session_scope
from katcha.domain import DiscoveryRunStatus
from katcha.services.acquisition import register_discovery_run
from katcha.services.discovery_health import (
    begin_source_poll,
    bind_source_poll_run,
    get_or_create_source_state,
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
    run_id: uuid.UUID | None
    adapter_key: str
    adapter_version: str
    workflow_id: str | None
    source_state_id: uuid.UUID
    source_key: str
    attempt_key: str
    allowed: bool
    reason: str
    next_eligible_poll_at: str | None = None


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


def _quota_limit(config: dict[str, Any]) -> int | None:
    raw = config.get("quota_limit_per_day")
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("quota_limit_per_day must be an integer") from exc
    if value < 1:
        raise ValueError("quota_limit_per_day must be positive")
    return value


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

    with session_scope() as session:
        watch = session.get(TopicWatchVersion, topic_watch_id)
        if watch is None:
            raise ValueError(f"topic watch version not found: {topic_watch_id}")
        if not watch.enabled:
            raise ValueError("topic watch is disabled")
        watch_key = watch.watch_key
        watch_version = watch.version
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

        quota_limit = _quota_limit(config)
        state = get_or_create_source_state(
            adapter_key=adapter_key,
            adapter_version=adapter_version,
            effective_query=query,
            topic_watch_id=topic_watch_id,
            quota_limit_per_day=quota_limit,
        )
        run_key = f"watch:{topic_watch_id}:{key}:{index}"
        decision = begin_source_poll(state.id, attempt_key=run_key)
        if not decision.allowed:
            runs.append(
                PreparedDiscoveryRun(
                    run_id=None,
                    adapter_key=adapter_key,
                    adapter_version=adapter_version,
                    workflow_id=None,
                    source_state_id=state.id,
                    source_key=state.source_key,
                    attempt_key=run_key,
                    allowed=False,
                    reason=decision.reason,
                    next_eligible_poll_at=(
                        decision.next_eligible_poll_at.isoformat()
                        if decision.next_eligible_poll_at
                        else None
                    ),
                )
            )
            continue

        run = register_discovery_run(
            adapter_key=adapter_key,
            adapter_version=adapter_version,
            query=query,
            idempotency_key=run_key,
            metadata={
                "topic_watch_id": str(topic_watch_id),
                "watch_key": watch_key,
                "watch_version": watch_version,
                "execution_key": key,
                "adapter_index": index,
                "source_state_id": str(state.id),
                "source_key": state.source_key,
                "source_attempt_key": run_key,
            },
        )
        with session_scope() as session:
            persisted = session.get(DiscoveryRun, run.id)
            if persisted is None:
                raise RuntimeError("discovery run disappeared during preparation")
            if persisted.status == DiscoveryRunStatus.QUEUED.value:
                persisted.cursor = dict(decision.cursor or {})
        bind_source_poll_run(
            state.id,
            attempt_key=run_key,
            discovery_run_id=run.id,
        )
        runs.append(
            PreparedDiscoveryRun(
                run_id=run.id,
                adapter_key=adapter_key,
                adapter_version=adapter_version,
                workflow_id=f"discovery-run-{run.id}",
                source_state_id=state.id,
                source_key=state.source_key,
                attempt_key=run_key,
                allowed=True,
                reason="ready" if not decision.reused else "reused_running_attempt",
            )
        )
    return runs
