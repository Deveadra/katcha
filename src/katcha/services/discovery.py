from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from katcha.acquisition_models import DiscoveryObservation, DiscoveryRun
from katcha.db import session_scope
from katcha.models import DomainEvent
from katcha.services.acquisition import register_discovery_candidate


def observe_discovery_candidate(
    *,
    source_url: str,
    adapter_key: str,
    discovery_run_id: uuid.UUID | None = None,
    external_id: str | None = None,
    title: str | None = None,
    creator: str | None = None,
    creator_url: str | None = None,
    provenance_confidence: float = 0.0,
    provenance_claims: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
):
    candidate = register_discovery_candidate(
        source_url=source_url,
        adapter_key=adapter_key,
        discovery_run_id=discovery_run_id,
        external_id=external_id,
        title=title,
        creator=creator,
        creator_url=creator_url,
        provenance_confidence=provenance_confidence,
        provenance_claims=provenance_claims,
        metadata=metadata,
    )
    if discovery_run_id is None:
        return candidate

    with session_scope() as session:
        if session.get(DiscoveryRun, discovery_run_id) is None:
            raise ValueError(f"discovery run not found: {discovery_run_id}")
        existing = session.scalar(
            select(DiscoveryObservation).where(
                DiscoveryObservation.discovery_run_id == discovery_run_id,
                DiscoveryObservation.discovery_candidate_id == candidate.id,
            )
        )
        if existing is None:
            observation = DiscoveryObservation(
                discovery_run_id=discovery_run_id,
                discovery_candidate_id=candidate.id,
                external_id=external_id,
                observation_metadata=metadata or {},
            )
            session.add(observation)
            session.flush()
            session.add(
                DomainEvent(
                    aggregate_type="discovery_candidate",
                    aggregate_id=str(candidate.id),
                    event_type="discovery_candidate.observed",
                    payload={
                        "discovery_candidate_id": str(candidate.id),
                        "discovery_run_id": str(discovery_run_id),
                        "discovery_observation_id": str(observation.id),
                        "adapter_key": adapter_key,
                    },
                )
            )
    return candidate
