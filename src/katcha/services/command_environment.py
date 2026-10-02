"""Read-only channel snapshot for planning; no credentials or arbitrary metadata."""

from __future__ import annotations

import uuid
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select

from katcha.acquisition_models import IngestionSource
from katcha.branding import brand_contract_for_profile_metadata
from katcha.db import session_scope
from katcha.edit_blueprint_models import ChannelEditBlueprintVersion
from katcha.editorial.rankings import get_ranking_format
from katcha.intelligence_models import AutomationPolicyVersion, ChannelStrategyVersion
from katcha.services.channel_brands import active_brand, brand_contract
from katcha.services.channel_profiles import ensure_active_profile
from katcha.trend_models import ChannelTrendWatchVersion


def command_environment(channel_profile_id: uuid.UUID) -> dict[str, Any]:
    with session_scope() as session:
        profile = ensure_active_profile(session, channel_profile_id)
        metadata = dict(profile.profile_metadata or {})
        strategy = session.scalar(
            select(ChannelStrategyVersion).where(
                ChannelStrategyVersion.channel_profile_id == profile.id,
                ChannelStrategyVersion.version == profile.active_strategy_version,
            )
        )
        policy = session.scalar(
            select(AutomationPolicyVersion).where(
                AutomationPolicyVersion.channel_profile_id == profile.id,
                AutomationPolicyVersion.version == profile.active_automation_version,
            )
        )
        watch = session.scalar(
            select(ChannelTrendWatchVersion)
            .where(
                ChannelTrendWatchVersion.channel_profile_id == profile.id,
            )
            .order_by(ChannelTrendWatchVersion.version.desc())
            .limit(1)
        )
        sources = list(
            session.scalars(
                select(IngestionSource)
                .where(
                    IngestionSource.enabled.is_(True),
                    or_(
                        IngestionSource.channel_profile_id == profile.id,
                        IngestionSource.channel_profile_id.is_(None),
                    ),
                )
                .order_by(IngestionSource.source_key)
                .limit(31)
            )
        )
        blueprints = list(
            session.scalars(
                select(ChannelEditBlueprintVersion)
                .where(
                    ChannelEditBlueprintVersion.channel_profile_id == profile.id,
                    ChannelEditBlueprintVersion.is_active.is_(True),
                )
                .order_by(ChannelEditBlueprintVersion.blueprint_key)
                .limit(13)
            )
        )
        brand_row = active_brand(session, profile)
        brand = (
            brand_contract(brand_row) if brand_row
            else brand_contract_for_profile_metadata(metadata)
        )
        allowed_counts: list[int] = []
        if brand and brand.editorial_format:
            with suppress(KeyError):
                allowed_counts = list(
                    get_ranking_format(
                        brand.editorial_format.key,
                        brand.editorial_format.version,
                    ).allowed_item_counts
                )
        return {
            "observed_at": datetime.now(UTC).isoformat(),
            "channel": {
                "id": str(profile.id),
                "status": profile.status,
                "name": str(metadata.get("channel_title") or metadata.get("name") or ""),
                "handle": str(metadata.get("channel_handle") or metadata.get("handle") or ""),
                "timezone": profile.timezone,
                "local_date": datetime.now(UTC)
                .astimezone(ZoneInfo(profile.timezone))
                .date()
                .isoformat(),
            },
            "constraints": {
                "automation_level": policy.level if policy else None,
                "monthly_hard_budget_usd": float(strategy.monthly_hard_budget_usd)
                if strategy
                else None,
                "ranked_item_counts": allowed_counts,
                "brand_configured": brand_row is not None,
                "publication_requires_native_approval": True,
            },
            "interests": list(watch.interests or [])[:20] if watch else [],
            "excluded_terms": list(watch.excluded_terms or [])[:20] if watch else [],
            "sources": [
                {
                    "id": str(row.id),
                    "name": row.name,
                    "platform": row.platform,
                    "shared": row.channel_profile_id is None,
                }
                for row in sources[:30]
            ],
            "sources_truncated": len(sources) > 30,
            "edit_blueprints": [
                {"key": row.blueprint_key, "version": row.version, "default": row.is_default}
                for row in blueprints[:12]
            ],
            "edit_blueprints_truncated": len(blueprints) > 12,
        }
