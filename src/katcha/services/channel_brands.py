from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from katcha.brand_models import ChannelBrandVersion
from katcha.branding import (
    ChannelBrandContract,
    brand_contract_for_profile_metadata,
    default_brand_contract,
    rank_snaxx_brand_v2,
    validate_brand_contract,
)
from katcha.intelligence_models import ChannelProfile
from katcha.models import DomainEvent
from katcha.services.channel_profiles import ensure_active_profile


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

    contract = brand_contract_for_profile_metadata(dict(profile.profile_metadata or {}))
    brand = ChannelBrandVersion(
        channel_profile_id=profile.id,
        version=1,
        brand_key=contract.brand_key,
        is_active=True,
        contract=contract.model_dump(mode="json"),
        brand_metadata={
            "created_by": "profile_bootstrap",
            "identity_resolution": "profile_metadata",
        },
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


def _lock_profile(session: Session, profile: ChannelProfile) -> ChannelProfile:
    locked = session.scalar(
        select(ChannelProfile)
        .where(ChannelProfile.id == profile.id)
        .with_for_update()
    )
    if locked is None:
        raise ValueError(f"channel profile not found: {profile.id}")
    return locked


def list_channel_brand_versions(
    channel_profile_id: uuid.UUID,
) -> list[ChannelBrandVersion]:
    from katcha.db import session_scope

    with session_scope() as session:
        profile = ensure_active_profile(session, channel_profile_id)
        ensure_active_brand(session, profile)
        rows = list(
            session.scalars(
                select(ChannelBrandVersion)
                .where(ChannelBrandVersion.channel_profile_id == profile.id)
                .order_by(ChannelBrandVersion.version.desc())
            )
        )
        for row in rows:
            session.expunge(row)
        return rows


def stage_brand_version(
    channel_profile_id: uuid.UUID,
    *,
    contract_payload: dict[str, Any],
    actor: str = "operator",
    hypothesis: str | None = None,
) -> ChannelBrandVersion:
    from katcha.db import session_scope

    contract = validate_brand_contract(contract_payload)
    with session_scope() as session:
        profile = ensure_active_profile(session, channel_profile_id)
        profile = _lock_profile(session, profile)
        current = ensure_active_brand(session, profile)
        latest_version = session.scalar(
            select(ChannelBrandVersion.version)
            .where(ChannelBrandVersion.channel_profile_id == profile.id)
            .order_by(ChannelBrandVersion.version.desc())
            .limit(1)
        )
        next_version = int(latest_version or current.version) + 1
        if contract.version != next_version:
            raise ValueError(f"next brand contract version must be {next_version}")

        row = ChannelBrandVersion(
            channel_profile_id=profile.id,
            version=next_version,
            brand_key=contract.brand_key,
            is_active=False,
            contract=contract.model_dump(mode="json"),
            brand_metadata={
                "actor": actor,
                "staged_from": current.version,
                "hypothesis": hypothesis,
            },
        )
        session.add(row)
        session.flush()
        session.add(
            DomainEvent(
                aggregate_type="channel_profile",
                aggregate_id=str(profile.id),
                event_type="channel_profile.brand_staged",
                payload={
                    "channel_profile_id": str(profile.id),
                    "brand_key": contract.brand_key,
                    "brand_version": row.version,
                    "active_brand_version": current.version,
                    "actor": actor,
                    "hypothesis": hypothesis,
                },
            )
        )
        session.refresh(row)
        session.expunge(row)
        return row


def activate_brand_version(
    channel_profile_id: uuid.UUID,
    *,
    version: int,
    actor: str = "operator",
) -> ChannelBrandVersion:
    from katcha.db import session_scope

    with session_scope() as session:
        profile = ensure_active_profile(session, channel_profile_id)
        profile = _lock_profile(session, profile)
        target = session.scalar(
            select(ChannelBrandVersion).where(
                ChannelBrandVersion.channel_profile_id == profile.id,
                ChannelBrandVersion.version == version,
            )
        )
        if target is None:
            raise ValueError(f"brand version not found for channel: {version}")
        brand_contract(target)

        current = active_brand(session, profile)
        if current is not None and current.id != target.id:
            current.is_active = False
            session.flush()
        target.is_active = True
        metadata = dict(target.brand_metadata or {})
        metadata["activated_by"] = actor
        metadata["supersedes"] = current.version if current else None
        target.brand_metadata = metadata
        session.flush()
        session.add(
            DomainEvent(
                aggregate_type="channel_profile",
                aggregate_id=str(profile.id),
                event_type="channel_profile.brand_activated",
                payload={
                    "channel_profile_id": str(profile.id),
                    "brand_key": target.brand_key,
                    "brand_version": target.version,
                    "supersedes": current.version if current else None,
                    "actor": actor,
                },
            )
        )
        session.refresh(target)
        session.expunge(target)
        return target


def builtin_brand_candidates(
    channel_profile_id: uuid.UUID,
) -> list[ChannelBrandContract]:
    from katcha.db import session_scope

    with session_scope() as session:
        profile = ensure_active_profile(session, channel_profile_id)
        current = brand_contract(ensure_active_brand(session, profile))

    candidates: list[ChannelBrandContract] = []
    ranksnaxx_v2 = rank_snaxx_brand_v2()
    if (
        current.brand_key == ranksnaxx_v2.brand_key
        and current.version < ranksnaxx_v2.version
    ):
        candidates.append(ranksnaxx_v2)
    return candidates
