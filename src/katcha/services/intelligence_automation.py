from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from katcha.acquisition_models import (
    DiscoveryCandidate,
    IntelligenceRecord,
)
from katcha.db import session_scope
from katcha.domain import AudioRightsStatus, GateStatus, RightsBasis
from katcha.models import DomainEvent, SourceItem
from katcha.orchestration.client import start_ingest_workflow
from katcha.services.acquisition import (
    assess_discovery_candidate,
    latest_rights_assessment,
    promote_discovery_candidate,
)
from katcha.services.discovery import observe_discovery_candidate
from katcha.services.trailer_passthrough import prepare_authorized_passthrough_source


@dataclass(frozen=True, slots=True)
class HandoffAdvanceResult:
    record_id: uuid.UUID
    candidate_id: uuid.UUID | None
    source_id: uuid.UUID | None
    workflow_id: str | None
    action: str
    reason: str | None = None


def _authorized_passthrough(record: IntelligenceRecord) -> tuple[bool, str | None]:
    payload = dict(record.payload or {})
    provenance = dict(record.provenance or {})
    if record.record_kind not in {"video", "clip"}:
        return False, "record is not a video/clip"
    if not record.source_url:
        return False, "record has no source_url"
    if str(payload.get("production_intent") or "") != "source_passthrough":
        return False, "record is not source_passthrough"
    if not bool(payload.get("operator_authorized")):
        return False, "record is not operator-authorized"
    if str(payload.get("authorization_scope") or "") != "official_trailer_repost":
        return False, "record authorization_scope is not official_trailer_repost"
    if not bool(
        provenance.get("official_channel_verified")
        or payload.get("official_source_verified")
    ):
        return False, "official source is not verified"
    return True, None


def _candidate_metadata(record: IntelligenceRecord) -> dict[str, Any]:
    payload = dict(record.payload or {})
    provenance = dict(record.provenance or {})
    return {
        "channel_profile_id": str(record.channel_profile_id),
        "intelligence_record_id": str(record.id),
        "intelligence_record_kind": record.record_kind,
        "intelligence_title": record.title,
        "intelligence_summary": record.summary,
        "intelligence_tags": list(record.tags or []),
        "intelligence_payload": payload,
        "source_type": "intelligence_handoff",
        "operator_authorized": bool(payload.get("operator_authorized")),
        "authorization_scope": str(payload.get("authorization_scope") or "") or None,
        "official_source_verified": bool(
            provenance.get("official_channel_verified")
            or payload.get("official_source_verified")
        ),
    }


def prepare_authorized_handoff_record(record_id: uuid.UUID) -> HandoffAdvanceResult:
    with session_scope() as session:
        record = session.get(IntelligenceRecord, record_id)
        if record is None:
            raise ValueError(f"intelligence record not found: {record_id}")
        eligible, reason = _authorized_passthrough(record)
        if not eligible:
            return HandoffAdvanceResult(
                record_id=record.id,
                candidate_id=None,
                source_id=None,
                workflow_id=None,
                action="skipped",
                reason=reason,
            )
        source_url = str(record.source_url)
        record_key = record.record_key
        title = record.title
        payload = dict(record.payload or {})
        provenance = dict(record.provenance or {})
        channel_profile_id = record.channel_profile_id
        creator = str(payload.get("creator") or payload.get("channel_name") or "").strip() or None
        creator_url = str(payload.get("creator_url") or "").strip() or None
        try:
            confidence = float(provenance.get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        metadata = _candidate_metadata(record)

    candidate = observe_discovery_candidate(
        source_url=source_url,
        adapter_key="operator_feed",
        external_id=record_key,
        title=title,
        creator=creator,
        creator_url=creator_url,
        provenance_confidence=max(0.0, min(confidence, 1.0)),
        provenance_claims={
            **provenance,
            "intelligence_record_id": str(record_id),
            "intelligence_record_kind": "video",
        },
        metadata=metadata,
    )

    # A corrected/revised handoff may point at a candidate that already existed.
    # Refresh its channel/intelligence metadata before assessment/promotion so an
    # earlier package cannot strand stale intent on the candidate.
    with session_scope() as session:
        stored = session.get(DiscoveryCandidate, candidate.id)
        if stored is None:
            raise RuntimeError("materialized discovery candidate disappeared")
        before = dict(stored.candidate_metadata or {})
        merged = {**before, **metadata}
        if merged != before:
            stored.candidate_metadata = merged
            stored.title = title or stored.title
            stored.creator = creator or stored.creator
            stored.creator_url = creator_url or stored.creator_url
            stored.provenance_claims = {
                **dict(stored.provenance_claims or {}),
                **provenance,
                "intelligence_record_id": str(record_id),
            }
            session.add(
                DomainEvent(
                    aggregate_type="discovery_candidate",
                    aggregate_id=str(stored.id),
                    event_type="discovery_candidate.handoff_refreshed",
                    payload={
                        "discovery_candidate_id": str(stored.id),
                        "intelligence_record_id": str(record_id),
                        "channel_profile_id": str(channel_profile_id),
                        "operator_authorized": True,
                    },
                )
            )
            session.flush()

        assessment = latest_rights_assessment(session, stored.id)
        already_eligible = bool(
            assessment is not None
            and assessment.production_eligible
            and assessment.operator_authorized
        )

    if not already_eligible:
        assess_discovery_candidate(
            candidate.id,
            rights_basis=RightsBasis.OPERATOR_AUTHORIZED,
            audio_status=AudioRightsStatus.ORIGINAL,
            originality_gate=GateStatus.CLEARED,
            risk_flags=[],
            operator_authorized=True,
            metadata={
                "authorization_source": "intelligence_handoff",
                "authorization_scope": "official_trailer_repost",
                "official_source_verified": True,
                "intelligence_record_id": str(record_id),
                "channel_profile_id": str(channel_profile_id),
            },
            actor="operator",
            reason="Standing operator authorization for verified official-trailer republication",
        )

    source = promote_discovery_candidate(
        candidate.id,
        actor="intelligence_handoff",
        for_review=False,
    )
    return HandoffAdvanceResult(
        record_id=record_id,
        candidate_id=candidate.id,
        source_id=source.id,
        workflow_id=source.workflow_id,
        action="ingest_queued" if source.clip_id is None else "already_ingested",
        reason=None,
    )


async def advance_processed_handoff_receipt(
    receipt: dict[str, Any] | None,
) -> list[HandoffAdvanceResult]:
    if not receipt or receipt.get("status") != "processed":
        return []
    results: list[HandoffAdvanceResult] = []
    for value in receipt.get("record_ids") or []:
        try:
            record_id = uuid.UUID(str(value))
        except (TypeError, ValueError):
            continue
        result = prepare_authorized_handoff_record(record_id)
        results.append(result)
        if result.source_id is not None and result.workflow_id and result.action == "ingest_queued":
            await start_ingest_workflow(str(result.source_id), result.workflow_id)
    return results


async def reconcile_authorized_handoff_records(
    *,
    limit: int = 200,
) -> list[HandoffAdvanceResult]:
    """Resume authorized trailer handoffs already persisted before this automation existed."""
    if limit < 1 or limit > 1000:
        raise ValueError("limit must be between 1 and 1000")
    with session_scope() as session:
        records = list(
            session.scalars(
                select(IntelligenceRecord)
                .where(IntelligenceRecord.status == "active")
                .order_by(IntelligenceRecord.updated_at.desc())
                .limit(limit)
            )
        )
        record_ids = [row.id for row in records if _authorized_passthrough(row)[0]]

    results: list[HandoffAdvanceResult] = []
    for record_id in record_ids:
        result = prepare_authorized_handoff_record(record_id)
        results.append(result)
        if result.source_id is None:
            continue
        if result.action == "ingest_queued" and result.workflow_id:
            await start_ingest_workflow(str(result.source_id), result.workflow_id)
        elif result.action == "already_ingested":
            prepare_authorized_passthrough_source(result.source_id)
    return results


def source_intelligence_record_id(source: SourceItem) -> uuid.UUID | None:
    value = (source.source_metadata or {}).get("intelligence_record_id")
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None
