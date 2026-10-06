"""Durable command identity and revalidation of captured control authority."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from katcha.api.control_auth import _credential_is_active
from katcha.config import get_settings
from katcha.db import session_scope
from katcha.goal_models import CommandGoal
from katcha.services.channel_profiles import ensure_active_profile
from katcha.services.command_history import create_command_thread, get_command_thread

TERMINAL_GOAL_STATES = {"completed", "blocked", "needs_input", "failed", "cancelled"}
MAX_GOAL_STEPS = 16


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def get_goal(goal_id: uuid.UUID) -> CommandGoal:
    with session_scope() as session:
        goal = session.get(CommandGoal, goal_id)
        if goal is None:
            raise ValueError("Saved request was not found")
        session.expunge(goal)
        return goal


def register_goal(command_id: uuid.UUID, request: dict, authority: dict) -> CommandGoal:
    actor = authority["actor"]
    channel_id = uuid.UUID(request["channel_profile_id"])
    request_hash = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()

    def existing():
        with session_scope() as session:
            row = session.scalar(
                select(CommandGoal).where(
                    CommandGoal.actor == actor,
                    CommandGoal.command_id == command_id,
                )
            )
            if row:
                if row.request_hash != request_hash:
                    raise ValueError(
                        "This request identity already belongs to a different instruction"
                    )
                session.expunge(row)
            return row

    row = existing()
    if row:
        return row
    with session_scope() as session:
        ensure_active_profile(session, channel_id)
    selected_resource_evidence(request)
    thread_id = request.get("thread_id")
    if thread_id:
        thread = get_command_thread(uuid.UUID(thread_id), channel_profile_id=channel_id)
        if thread.status != "active":
            raise ValueError("This conversation is archived")
    else:
        thread = create_command_thread(
            channel_profile_id=channel_id, actor=actor, title=request["prompt"]
        )
    try:
        with session_scope() as session:
            row = CommandGoal(
                id=uuid.uuid4(),
                command_id=command_id,
                channel_profile_id=channel_id,
                thread_id=thread.id,
                actor=actor,
                request_hash=request_hash,
                request=request,
                authority=authority,
                authorization={},
                status="queued",
                step_count=0,
                observations=[],
                result={},
                deadline=datetime.now(UTC) + timedelta(hours=1),
                summary="Request saved; waiting to start.",
            )
            session.add(row)
            session.flush()
            session.expunge(row)
            return row
    except IntegrityError:
        row = existing()
        if row is None:
            raise
        return row


def resolve_goal_authority(goal: CommandGoal) -> tuple[set[str], str | None]:
    """No credential is stored in receipts. Revoke/expiry/channel changes stop work."""
    settings = get_settings()
    captured = dict(goal.authority)
    actor = captured["actor"]
    fingerprint = captured.get("credential_fingerprint")
    token = None
    current_channels: set[str]
    if captured.get("principal_name"):
        principal = next(
            (p for p in settings.control_principals if p.name == captured["principal_name"]), None
        )
        if principal is None:
            raise ValueError("The control principal for this request was revoked")
        credential = next(
            (
                c
                for c in principal.resolved_credentials()
                if c.id == captured.get("credential_id")
                and _credential_is_active(c, now=datetime.now(UTC))
                and hashlib.sha256(c.token.get_secret_value().encode()).hexdigest()[:12]
                == fingerprint
            ),
            None,
        )
        if credential is None:
            raise ValueError("The credential for this request expired or was revoked")
        if actor != f"control-principal:{principal.name}":
            raise ValueError("Saved request authority does not match its principal")
        scopes = set(principal.scopes)
        current_channels = set(principal.channel_profile_ids)
        token = credential.token.get_secret_value()
    elif actor == "local-development":
        if (
            settings.env == "production"
            or settings.control_api_token
            or settings.control_principals
        ):
            raise ValueError("Local development authority is no longer available")
        scopes, current_channels = {"*"}, {"*"}
    else:
        secret = settings.control_api_token
        token = secret.get_secret_value() if secret else None
        if settings.control_principals or not token:
            raise ValueError("The saved control credential is no longer available")
        digest = hashlib.sha256(token.encode()).hexdigest()[:12]
        if digest != fingerprint or actor != f"control-token:{digest}":
            raise ValueError("The saved control credential has changed")
        scopes, current_channels = settings.resolved_control_scopes(), {"*"}
    captured_scopes = set(captured["scopes"])
    effective = (
        scopes
        if "*" in captured_scopes
        else captured_scopes
        if "*" in scopes
        else scopes & captured_scopes
    )
    for allowed in (current_channels, set(captured["channel_ids"])):
        if "*" not in allowed and str(goal.channel_profile_id) not in allowed:
            raise ValueError("Channel access for this request is no longer available")
    if "*" not in effective and not {"ai:read", "ai:command"} <= effective:
        raise ValueError("Command permissions for this request are no longer available")
    return effective, token


def selected_resource_evidence(request: dict) -> list[dict]:
    from katcha.services.command_resources import resolve_command_resources

    channel_id = uuid.UUID(request["channel_profile_id"])
    refs = []
    for row in request.get("resource_refs", []):
        kind = row["kind"]
        resource_id = uuid.UUID(row["id"])
        selector = row.get("selector")
        revision = row.get("revision")
        refs.append(
            (kind, resource_id, selector, revision)
            if selector is not None or revision is not None
            else (kind, resource_id)
        )
    refs.extend(("clip", uuid.UUID(value)) for value in request.get("selected_clip_ids", []))
    if request.get("selected_production_id"):
        refs.append(("production", uuid.UUID(request["selected_production_id"])))
    refs = list(dict.fromkeys(refs))
    evidence = []
    for offset in range(0, len(refs), 8):
        evidence.extend(resolve_command_resources(channel_id, refs[offset : offset + 8]))
    return evidence
