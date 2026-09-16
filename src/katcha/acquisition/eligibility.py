from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from katcha.acquisition_models import DiscoveryCandidate, RightsAssessment
from katcha.models import SourceItem


def production_eligible_clip_ids(
    session: Session,
    clip_ids: list[uuid.UUID] | set[uuid.UUID],
) -> set[uuid.UUID]:
    requested = set(clip_ids)
    if not requested:
        return set()

    candidate_rows = list(
        session.execute(
            select(DiscoveryCandidate.id, SourceItem.clip_id)
            .join(SourceItem, DiscoveryCandidate.source_item_id == SourceItem.id)
            .where(SourceItem.clip_id.in_(requested))
        )
    )
    if not candidate_rows:
        return requested

    candidate_to_clip = {
        candidate_id: clip_id
        for candidate_id, clip_id in candidate_rows
        if clip_id is not None
    }
    managed_clip_ids = set(candidate_to_clip.values())
    latest: dict[uuid.UUID, RightsAssessment] = {}
    if candidate_to_clip:
        assessments = list(
            session.scalars(
                select(RightsAssessment)
                .where(
                    RightsAssessment.discovery_candidate_id.in_(candidate_to_clip)
                )
                .order_by(
                    RightsAssessment.discovery_candidate_id,
                    RightsAssessment.version.asc(),
                )
            )
        )
        for assessment in assessments:
            latest[assessment.discovery_candidate_id] = assessment

    eligible_managed = {
        clip_id
        for candidate_id, clip_id in candidate_to_clip.items()
        if candidate_id in latest and latest[candidate_id].production_eligible
    }
    legacy_unmanaged = requested - managed_clip_ids
    return legacy_unmanaged | eligible_managed
