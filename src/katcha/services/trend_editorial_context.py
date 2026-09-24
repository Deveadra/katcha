"""Freeze qualified, bounded source context for the existing paid editorial pipeline."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from katcha.trend_models import ChannelTrendWatchVersion, TrendEvidencePacket, TrendOpportunity


def freeze_trend_context(
    session: Session, channel_id: uuid.UUID, opportunity_id: uuid.UUID
) -> dict:
    opportunity = session.get(TrendOpportunity, opportunity_id)
    if opportunity is None or opportunity.channel_profile_id != channel_id:
        raise ValueError("trend opportunity not found in this channel")
    expiry = opportunity.expires_at
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    if expiry <= datetime.now(UTC):
        raise ValueError("trend opportunity has expired; refresh before planning")
    watch = session.scalar(
        select(ChannelTrendWatchVersion)
        .where(ChannelTrendWatchVersion.channel_profile_id == channel_id)
        .order_by(ChannelTrendWatchVersion.version.desc())
        .limit(1)
    )
    if watch is None or watch.version != opportunity.watch_version:
        raise ValueError("trend watch changed; refresh before planning")
    if (
        opportunity.opportunity_score < watch.opportunity_threshold
        or opportunity.confidence < watch.min_confidence
    ):
        raise ValueError("trend opportunity does not meet channel qualification thresholds")
    packet = session.scalar(
        select(TrendEvidencePacket)
        .where(TrendEvidencePacket.trend_opportunity_id == opportunity_id)
        .order_by(TrendEvidencePacket.version.desc())
        .limit(1)
    )
    if packet is None:
        raise ValueError("qualified trend evidence packet is required before planning")
    return packet_context(opportunity, packet)


def packet_context(opportunity: TrendOpportunity, packet: TrendEvidencePacket) -> dict:
    # Explicit allowlist and limits: no arbitrary metadata or executable source instructions.
    source_keys = (
        "signal_id",
        "provider_key",
        "source_kind",
        "source_name",
        "canonical_url",
        "title",
        "published_at",
        "observed_at",
    )
    sources = [
        {key: str(source[key])[:1200] for key in source_keys if source.get(key) is not None}
        for source in (packet.sources or [])[:12]
    ]
    claims = [
        {
            key: str(claim[key])[:600]
            for key in ("text", "signal_id", "source_url")
            if claim.get(key) is not None
        }
        for claim in (packet.claims or [])[:12]
    ]
    context = {
        "schema_version": "trend-editorial-context-v1",
        "opportunity_id": str(opportunity.id),
        "packet_id": str(packet.id),
        "packet_sha256": packet.packet_sha256,
        "packet_version": packet.version,
        "generated_at": packet.generated_at.isoformat(),
        "thesis": packet.thesis[:1500],
        "why_now": [str(x)[:400] for x in packet.why_now[:8]],
        "sources": sources,
        "source_claims": claims,
        "sources_truncated": len(packet.sources or []) > len(sources),
        "claims_truncated": len(packet.claims or []) > len(claims),
        "source_claims_verified": False,
        "media_reuse_permission": "not_inferred",
    }

    # Bound total context, not just individual fields. Retain the full packet's identity.
    while len(json.dumps(context, ensure_ascii=False)) > 12000:
        if context["source_claims"]:
            context["source_claims"].pop()
            context["claims_truncated"] = True
        elif context["sources"]:
            context["sources"].pop()
            context["sources_truncated"] = True
        else:
            break
    return context
