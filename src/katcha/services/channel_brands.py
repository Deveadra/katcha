from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from katcha.brand_models import ChannelBrandVersion
from katcha.branding import ChannelBrandContract, default_brand_contract, validate_brand_contract
from katcha.intelligence_models import ChannelProfile
from katcha.models import DomainEvent


def active_brand(
    session: Session,
    profile: ChannelProfile,
) -> ChannelBrandVersion | None:
    return session.scalar(
        select(ChannelBrandVersion)
        .where(
            ChannelBrandVersion.channel_profile_id == profile.id,
            ChannelBrandVersion.is_active.is_(True),
        )
        .order_by(ChannelBrandVersion.version.desc())
    )


def ensure_active_brand(
    session: Session,
    profile: ChannelProfile,
) -> ChannelBrandVersion:
    existing = active_brand(session, profile)
    if existing is not None:
        validate_brand_contract(dict(existing.contract or {}))
        return existing

    contract = default_brand_contract()
    brand = ChannelBrandVersion(
        channel_profile_id=profile.id,
        version=1,
        brand_key=contract.brand_key,
        is_active=True,
        contract=contract.model_dump(mode="json"),
        brand_metadata={"created_by": "profile_bootstrap"},
    )
    session.add(brand)
    session.flush()
    return brand


def brand_contract(row: ChannelBrandVersion) -> ChannelBrandContract:
    contract = validate_brand_contract(dict(row.contract or {}))
    if contract.brand_key != row.brand_key or contract.version != row.version:
        raise RuntimeError("brand row identity does not match its stored contract")
    return contract


def create_brand_version(
    session: Session,
    profile: ChannelProfile,
    *,
    contract_payload: dict[str, Any],
    actor: str = "operator",
    hypothesis: str | None = None,
) -> ChannelBrandVersion:
    current = ensure_active_brand(session, profile)
    contract = validate_brand_contract(contract_payload)
    next_version = current.version + 1
    if contract.version != next_version:
        raise ValueError(f"next brand contract version must be {next_version}")

    current.is_active = False
    row = ChannelBrandVersion(
        channel_profile_id=profile.id,
        version=next_version,
        brand_key=contract.brand_key,
        is_active=True,
        contract=contract.model_dump(mode="json"),
        brand_metadata={
            "actor": actor,
            "supersedes": current.version,
            "hypothesis": hypothesis,
        },
    )
    session.add(row)
    session.add(
        DomainEvent(
            aggregate_type="channel_profile",
            aggregate_id=str(profile.id),
            event_type="channel_profile.brand_updated",
            payload={
                "channel_profile_id": str(profile.id),
                "brand_key": contract.brand_key,
                "brand_version": next_version,
                "supersedes": current.version,
                "actor": actor,
                "hypothesis": hypothesis,
            },
        )
    )
    session.flush()
    return row


def brand_for_channel(
    session: Session,
    channel_profile_id: uuid.UUID | None,
) -> tuple[ChannelBrandContract, int]:
    if channel_profile_id is None:
        contract = default_brand_contract()
        return contract, contract.version

    profile = session.get(ChannelProfile, channel_profile_id)
    if profile is None:
        raise ValueError(f"channel profile not found: {channel_profile_id}")
    row = ensure_active_brand(session, profile)
    return brand_contract(row), row.version
