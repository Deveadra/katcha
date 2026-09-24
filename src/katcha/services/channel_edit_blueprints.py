from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from katcha.edit_blueprint_models import ChannelEditBlueprintVersion
from katcha.editing.blueprints import (
    EditBlueprintContract,
    get_edit_blueprint,
    persona_commentary_v1,
)
from katcha.intelligence_models import ChannelProfile
from katcha.models import DomainEvent
from katcha.services.channel_profiles import ensure_active_profile


def edit_blueprint_contract(
    row: ChannelEditBlueprintVersion,
) -> EditBlueprintContract:
    contract = EditBlueprintContract.model_validate(dict(row.contract or {}))
    if contract.key != row.blueprint_key:
        raise RuntimeError("edit blueprint row key does not match stored contract")
    if contract.version != row.contract_version:
        raise RuntimeError("edit blueprint contract version does not match stored row")
    return contract


def _lock_profile(session: Session, profile: ChannelProfile) -> ChannelProfile:
    locked = session.scalar(
        select(ChannelProfile)
        .where(ChannelProfile.id == profile.id)
        .with_for_update()
    )
    if locked is None:
        raise ValueError(f"channel profile not found: {profile.id}")
    return locked


def _bootstrap_contract(profile: ChannelProfile) -> EditBlueprintContract:
    metadata = dict(profile.profile_metadata or {})
    key = str(metadata.get("default_edit_blueprint_key") or "").strip()
    contract_version = str(
        metadata.get("default_edit_blueprint_contract_version") or ""
    ).strip()
    if key:
        if not contract_version:
            contract_version = "1.0.0"
        try:
            return get_edit_blueprint(key, contract_version)
        except KeyError as exc:
            raise ValueError(
                f"channel default edit blueprint is unknown: {key}@{contract_version}"
            ) from exc
    return persona_commentary_v1()


def _active_family(
    session: Session,
    profile: ChannelProfile,
    blueprint_key: str,
) -> ChannelEditBlueprintVersion | None:
    return session.scalar(
        select(ChannelEditBlueprintVersion)
        .where(
            ChannelEditBlueprintVersion.channel_profile_id == profile.id,
            ChannelEditBlueprintVersion.blueprint_key == blueprint_key,
            ChannelEditBlueprintVersion.is_active.is_(True),
        )
        .order_by(ChannelEditBlueprintVersion.version.desc())
        .limit(1)
    )


def _default_row(
    session: Session,
    profile: ChannelProfile,
) -> ChannelEditBlueprintVersion | None:
    return session.scalar(
        select(ChannelEditBlueprintVersion)
        .where(
            ChannelEditBlueprintVersion.channel_profile_id == profile.id,
            ChannelEditBlueprintVersion.is_default.is_(True),
        )
        .order_by(ChannelEditBlueprintVersion.version.desc())
        .limit(1)
    )


def _next_version(
    session: Session,
    profile: ChannelProfile,
    blueprint_key: str,
) -> int:
    current = session.scalar(
        select(func.max(ChannelEditBlueprintVersion.version)).where(
            ChannelEditBlueprintVersion.channel_profile_id == profile.id,
            ChannelEditBlueprintVersion.blueprint_key == blueprint_key,
        )
    )
    return int(current or 0) + 1


def _clear_default(session: Session, profile: ChannelProfile) -> None:
    rows = list(
        session.scalars(
            select(ChannelEditBlueprintVersion).where(
                ChannelEditBlueprintVersion.channel_profile_id == profile.id,
                ChannelEditBlueprintVersion.is_default.is_(True),
            )
        )
    )
    for row in rows:
        row.is_default = False


def ensure_default_edit_blueprint(
    session: Session,
    profile: ChannelProfile,
) -> ChannelEditBlueprintVersion:
    profile = _lock_profile(session, profile)
    existing = _default_row(session, profile)
    if existing is not None:
        if not existing.is_active:
            raise RuntimeError("channel default edit blueprint is not active")
        edit_blueprint_contract(existing)
        return existing

    contract = _bootstrap_contract(profile)
    active = _active_family(session, profile, contract.key)
    if active is not None:
        active.is_default = True
        session.flush()
        return active

    row = ChannelEditBlueprintVersion(
        channel_profile_id=profile.id,
        blueprint_key=contract.key,
        version=_next_version(session, profile, contract.key),
        contract_version=contract.version,
        is_active=True,
        is_default=True,
        contract=contract.model_dump(mode="json"),
        blueprint_metadata={
            "created_by": "profile_bootstrap",
            "identity_resolution": "profile_metadata",
        },
    )
    session.add(row)
    session.flush()
    session.add(
        DomainEvent(
            aggregate_type="channel_profile",
            aggregate_id=str(profile.id),
            event_type="channel_profile.edit_blueprint_created",
            payload={
                "channel_profile_id": str(profile.id),
                "blueprint_key": row.blueprint_key,
                "blueprint_version": row.version,
                "contract_version": row.contract_version,
                "is_default": True,
                "actor": "profile_bootstrap",
            },
        )
    )
    return row


def blueprint_for_channel(
    session: Session,
    channel_profile_id: uuid.UUID | None,
    *,
    blueprint_key: str | None = None,
) -> tuple[EditBlueprintContract, int | None]:
    if channel_profile_id is None:
        if blueprint_key:
            try:
                contract = get_edit_blueprint(blueprint_key, "1.0.0")
            except KeyError as exc:
                raise ValueError(f"unknown edit blueprint: {blueprint_key}") from exc
        else:
            contract = persona_commentary_v1()
        return contract, None

    profile = ensure_active_profile(session, channel_profile_id)
    if blueprint_key:
        row = _active_family(session, profile, blueprint_key.strip())
        if row is None:
            raise ValueError(
                f"channel has no active edit blueprint family: {blueprint_key}"
            )
    else:
        row = ensure_default_edit_blueprint(session, profile)
    return edit_blueprint_contract(row), row.version


def list_channel_edit_blueprints(
    channel_profile_id: uuid.UUID,
) -> list[ChannelEditBlueprintVersion]:
    from katcha.db import session_scope

    with session_scope() as session:
        profile = ensure_active_profile(session, channel_profile_id)
        ensure_default_edit_blueprint(session, profile)
        rows = list(
            session.scalars(
                select(ChannelEditBlueprintVersion)
                .where(ChannelEditBlueprintVersion.channel_profile_id == profile.id)
                .order_by(
                    ChannelEditBlueprintVersion.blueprint_key,
                    ChannelEditBlueprintVersion.version.desc(),
                )
            )
        )
        for row in rows:
            session.expunge(row)
        return rows


def create_edit_blueprint_version(
    channel_profile_id: uuid.UUID,
    *,
    contract_payload: dict[str, Any],
    actor: str = "operator",
    set_default: bool = False,
) -> ChannelEditBlueprintVersion:
    from katcha.db import session_scope

    contract = EditBlueprintContract.model_validate(contract_payload)
    with session_scope() as session:
        profile = ensure_active_profile(session, channel_profile_id)
        profile = _lock_profile(session, profile)
        current_default = _default_row(session, profile)
        current_active = _active_family(session, profile, contract.key)
        carry_default = bool(current_active and current_active.is_default)
        if current_active is not None:
            current_active.is_active = False
            current_active.is_default = False
            session.flush()

        should_default = set_default or carry_default or current_default is None
        if should_default:
            _clear_default(session, profile)
            session.flush()

        version = _next_version(session, profile, contract.key)
        row = ChannelEditBlueprintVersion(
            channel_profile_id=profile.id,
            blueprint_key=contract.key,
            version=version,
            contract_version=contract.version,
            is_active=True,
            is_default=should_default,
            contract=contract.model_dump(mode="json"),
            blueprint_metadata={
                "actor": actor,
                "supersedes": current_active.version if current_active else None,
            },
        )
        session.add(row)
        session.flush()
        session.add(
            DomainEvent(
                aggregate_type="channel_profile",
                aggregate_id=str(profile.id),
                event_type="channel_profile.edit_blueprint_created",
                payload={
                    "channel_profile_id": str(profile.id),
                    "blueprint_key": row.blueprint_key,
                    "blueprint_version": row.version,
                    "contract_version": row.contract_version,
                    "supersedes": current_active.version if current_active else None,
                    "is_default": row.is_default,
                    "actor": actor,
                },
            )
        )
        session.refresh(row)
        session.expunge(row)
        return row


def activate_edit_blueprint_version(
    channel_profile_id: uuid.UUID,
    *,
    blueprint_key: str,
    version: int,
    actor: str = "operator",
    set_default: bool = False,
) -> ChannelEditBlueprintVersion:
    from katcha.db import session_scope

    with session_scope() as session:
        profile = ensure_active_profile(session, channel_profile_id)
        profile = _lock_profile(session, profile)
        target = session.scalar(
            select(ChannelEditBlueprintVersion).where(
                ChannelEditBlueprintVersion.channel_profile_id == profile.id,
                ChannelEditBlueprintVersion.blueprint_key == blueprint_key,
                ChannelEditBlueprintVersion.version == version,
            )
        )
        if target is None:
            raise ValueError(
                f"edit blueprint version not found for channel: {blueprint_key}@{version}"
            )
        edit_blueprint_contract(target)

        current = _active_family(session, profile, blueprint_key)
        carry_default = bool(current and current.is_default)
        if current is not None and current.id != target.id:
            current.is_active = False
            current.is_default = False
            session.flush()

        should_default = set_default or carry_default or _default_row(session, profile) is None
        if should_default:
            _clear_default(session, profile)
            session.flush()
        target.is_active = True
        target.is_default = should_default
        session.flush()
        session.add(
            DomainEvent(
                aggregate_type="channel_profile",
                aggregate_id=str(profile.id),
                event_type="channel_profile.edit_blueprint_activated",
                payload={
                    "channel_profile_id": str(profile.id),
                    "blueprint_key": target.blueprint_key,
                    "blueprint_version": target.version,
                    "contract_version": target.contract_version,
                    "is_default": target.is_default,
                    "actor": actor,
                },
            )
        )
        session.refresh(target)
        session.expunge(target)
        return target
