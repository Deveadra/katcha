#!/usr/bin/env python3
"""Fail-closed operational acceptance for Katcha's external control plane.

This runner is intentionally conservative. It proves that a real named operator
and a distinct named Aerith principal can negotiate the control contract, consume
a principal-bound event stream, submit a Katcha AI command, explicitly confirm a
safe action, observe the resulting workflow settle, and acknowledge the terminal
event.

It never creates or publishes media. The controlled mutation is a channel
intelligence refresh.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass
from typing import Any

import httpx


REQUIRED_AERITH_SCOPES = {
    "ai:read",
    "ai:command",
    "channels:read",
    "events:read",
    "events:ack",
    "intelligence:write",
}
ACTION_TYPE = "refresh_channel_intelligence"
SUCCESS_STATE = "completed"
LIFECYCLE_EVENTS = {
    "command_center.proposal_created",
    "command_center.action_confirmed",
    "command_center.action_executed",
    "command_center.workflow_started",
    "command_center.workflow_completed",
}


class AcceptanceError(RuntimeError):
    """Raised when the live control-plane acceptance fails closed."""


@dataclass(frozen=True, slots=True)
class Config:
    base_url: str
    channel_profile_id: uuid.UUID
    operator_token: str
    aerith_token: str
    operator_principal: str
    aerith_principal: str
    consumer_key: str
    request_timeout: float
    settle_timeout: float
    poll_seconds: float
    allow_fixture_ai: bool
    allow_aerith_all_channels: bool


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _decode(response: httpx.Response, context: str) -> Any:
    try:
        payload = response.json()
    except ValueError as exc:
        raise AcceptanceError(
            f"{context} returned non-JSON HTTP {response.status_code}"
        ) from exc
    if not response.is_success:
        detail = payload.get("detail") if isinstance(payload, dict) else payload
        raise AcceptanceError(
            f"{context} failed with HTTP {response.status_code}: {detail}"
        )
    return payload


def _get(
    client: httpx.Client,
    path: str,
    token: str,
    *,
    params: dict[str, object] | None = None,
) -> Any:
    response = client.get(path, headers=_auth(token), params=params)
    return _decode(response, f"GET {path}")


def _post(
    client: httpx.Client,
    path: str,
    token: str,
    *,
    body: dict[str, object],
) -> Any:
    response = client.post(path, headers=_auth(token), json=body)
    return _decode(response, f"POST {path}")


def _validate_session(
    payload: dict[str, Any],
    *,
    expected_principal: str,
    required_scopes: set[str],
    channel_profile_id: uuid.UUID,
    allow_all_channels: bool,
) -> None:
    if payload.get("authentication_mode") != "named_principal":
        raise AcceptanceError(
            f"{expected_principal} did not authenticate as a named principal"
        )
    if payload.get("principal_name") != expected_principal:
        raise AcceptanceError(
            f"expected principal {expected_principal!r}, got "
            f"{payload.get('principal_name')!r}"
        )

    actor = str(payload.get("actor") or "")
    if actor != f"control-principal:{expected_principal}":
        raise AcceptanceError(
            f"{expected_principal} returned unexpected actor {actor!r}"
        )

    credential = payload.get("credential")
    if not isinstance(credential, dict):
        raise AcceptanceError(
            f"{expected_principal} session did not report credential metadata"
        )
    if not credential.get("id") or not credential.get("fingerprint"):
        raise AcceptanceError(
            f"{expected_principal} credential metadata is incomplete"
        )

    scopes = {str(value) for value in payload.get("scopes") or []}
    if "*" not in scopes:
        missing = sorted(required_scopes - scopes)
        if missing:
            raise AcceptanceError(
                f"{expected_principal} is missing required scopes: "
                + ", ".join(missing)
            )

    access = payload.get("channel_access")
    if not isinstance(access, dict):
        raise AcceptanceError(
            f"{expected_principal} session did not report channel access"
        )
    all_channels = bool(access.get("all_channels"))
    if all_channels:
        if not allow_all_channels:
            raise AcceptanceError(
                f"{expected_principal} has wildcard channel access; "
                "use a channel-scoped credential for acceptance or pass the "
                "explicit override"
            )
        return

    allowed = {str(value) for value in access.get("channel_profile_ids") or []}
    if str(channel_profile_id) not in allowed:
        raise AcceptanceError(
            f"{expected_principal} is not authorized for channel "
            f"{channel_profile_id}"
        )


def _validate_distinct_sessions(
    operator_session: dict[str, Any],
    aerith_session: dict[str, Any],
) -> None:
    operator_actor = str(operator_session.get("actor") or "")
    aerith_actor = str(aerith_session.get("actor") or "")
    if not operator_actor or not aerith_actor or operator_actor == aerith_actor:
        raise AcceptanceError(
            "operator and Aerith must resolve to distinct named control actors"
        )

    operator_credential = operator_session.get("credential") or {}
    aerith_credential = aerith_session.get("credential") or {}
    if (
        operator_credential.get("fingerprint")
        and operator_credential.get("fingerprint")
        == aerith_credential.get("fingerprint")
    ):
        raise AcceptanceError(
            "operator and Aerith appear to be using the same bearer credential"
        )


def _event_params(config: Config, *, limit: int = 100) -> dict[str, object]:
    return {
        "consumer_key": config.consumer_key,
        "channel_profile_id": str(config.channel_profile_id),
        "limit": limit,
    }


def _ack(
    client: httpx.Client,
    config: Config,
    event_id: str,
    *,
    metadata: dict[str, object] | None = None,
) -> dict[str, Any]:
    payload = _post(
        client,
        f"/v1/control/events/{event_id}/ack",
        config.aerith_token,
        body={
            "consumer_key": config.consumer_key,
            "channel_profile_id": str(config.channel_profile_id),
            "metadata": metadata or {"surface": "control-plane-live-acceptance"},
        },
    )
    if not isinstance(payload, dict):
        raise AcceptanceError("event acknowledgement returned an invalid payload")
    return payload


def _prime_cursor(client: httpx.Client, config: Config) -> int:
    """Advance a dedicated acceptance cursor to the current channel-event tail."""

    drained = 0
    for _ in range(100):
        payload = _get(
            client,
            "/v1/control/events",
            config.aerith_token,
            params=_event_params(config, limit=500),
        )
        if not isinstance(payload, list):
            raise AcceptanceError("event stream returned an invalid payload")
        if not payload:
            return drained
        last = payload[-1]
        event_id = str(last.get("id") or "")
        if not event_id:
            raise AcceptanceError("event stream returned an event without an ID")
        _ack(
            client,
            config,
            event_id,
            metadata={
                "surface": "control-plane-live-acceptance",
                "phase": "baseline",
            },
        )
        drained += len(payload)
    raise AcceptanceError(
        "could not reach the current event-stream tail after 100 batches"
    )


def _correlated_events(
    events: list[dict[str, Any]],
    proposal_id: str,
) -> list[dict[str, Any]]:
    return [
        event
        for event in events
        if str((event.get("payload") or {}).get("proposal_id") or "")
        == proposal_id
    ]


def _wait_for_activity(
    client: httpx.Client,
    config: Config,
    proposal_id: str,
) -> dict[str, Any]:
    deadline = time.monotonic() + config.settle_timeout
    last: dict[str, Any] | None = None

    while time.monotonic() < deadline:
        payload = _get(
            client,
            f"/v1/ai/actions/{proposal_id}/activity",
            config.aerith_token,
            params={"limit": 100},
        )
        if not isinstance(payload, dict):
            raise AcceptanceError("action activity returned an invalid payload")
        last = payload

        if payload.get("settled"):
            state = str(payload.get("state") or "")
            if state != SUCCESS_STATE:
                raise AcceptanceError(
                    f"command workflow settled in unexpected state {state!r}"
                )
            return payload

        time.sleep(config.poll_seconds)

    state = str((last or {}).get("state") or "unknown")
    raise AcceptanceError(
        f"command workflow did not settle within the configured timeout; "
        f"last state={state!r}"
    )


def _verify_events(
    client: httpx.Client,
    config: Config,
    proposal_id: str,
    aerith_actor: str,
) -> tuple[list[dict[str, Any]], str]:
    deadline = time.monotonic() + config.settle_timeout
    seen: list[dict[str, Any]] = []

    while time.monotonic() < deadline:
        payload = _get(
            client,
            "/v1/control/events",
            config.aerith_token,
            params=_event_params(config, limit=500),
        )
        if not isinstance(payload, list):
            raise AcceptanceError("event stream returned an invalid payload")

        seen = _correlated_events(payload, proposal_id)
        event_types = {str(item.get("event_type") or "") for item in seen}
        if LIFECYCLE_EVENTS.issubset(event_types):
            for item in seen:
                if item.get("event_type") not in {
                    "command_center.action_confirmed",
                    "command_center.action_executed",
                    "command_center.workflow_started",
                    "command_center.workflow_completed",
                }:
                    continue
                actor = str((item.get("payload") or {}).get("actor") or "")
                if actor and actor != aerith_actor:
                    raise AcceptanceError(
                        f"event {item.get('event_type')} was attributed to "
                        f"unexpected actor {actor!r}"
                    )

            terminal = next(
                item
                for item in reversed(seen)
                if item.get("event_type") == "command_center.workflow_completed"
            )
            terminal_id = str(terminal.get("id") or "")
            if not terminal_id:
                raise AcceptanceError("terminal lifecycle event has no event ID")
            return seen, terminal_id

        time.sleep(config.poll_seconds)

    found = sorted({str(item.get("event_type") or "") for item in seen})
    missing = sorted(LIFECYCLE_EVENTS - set(found))
    raise AcceptanceError(
        "event stream did not expose the complete command lifecycle; "
        f"missing={missing}, found={found}"
    )


def run(config: Config) -> dict[str, object]:
    if config.operator_token == config.aerith_token:
        raise AcceptanceError(
            "operator and Aerith tokens must be distinct before acceptance"
        )

    with httpx.Client(
        base_url=config.base_url.rstrip("/"),
        timeout=config.request_timeout,
        follow_redirects=False,
    ) as client:
        operator_session = _get(
            client,
            "/v1/control/session",
            config.operator_token,
        )
        aerith_session = _get(
            client,
            "/v1/control/session",
            config.aerith_token,
        )
        if not isinstance(operator_session, dict) or not isinstance(
            aerith_session, dict
        ):
            raise AcceptanceError("control session returned an invalid payload")

        _validate_session(
            operator_session,
            expected_principal=config.operator_principal,
            required_scopes=set(),
            channel_profile_id=config.channel_profile_id,
            allow_all_channels=True,
        )
        _validate_session(
            aerith_session,
            expected_principal=config.aerith_principal,
            required_scopes=REQUIRED_AERITH_SCOPES,
            channel_profile_id=config.channel_profile_id,
            allow_all_channels=config.allow_aerith_all_channels,
        )
        _validate_distinct_sessions(operator_session, aerith_session)

        readiness = _get(
            client,
            "/v1/ai/readiness",
            config.aerith_token,
        )
        if not isinstance(readiness, dict):
            raise AcceptanceError("AI readiness returned an invalid payload")
        ai_live = bool(readiness.get("live"))
        if not ai_live and not config.allow_fixture_ai:
            raise AcceptanceError(
                "Katcha AI is not in live-provider mode; configure Live AI or "
                "use --allow-fixture-ai only for a diagnostic acceptance"
            )

        baseline_count = _prime_cursor(client, config)

        command = _post(
            client,
            "/v1/ai/command",
            config.aerith_token,
            body={
                "channel_profile_id": str(config.channel_profile_id),
                "prompt": (
                    "Look at yesterday's performance and tell me what editing "
                    "behavior should change."
                ),
            },
        )
        if not isinstance(command, dict):
            raise AcceptanceError("Katcha AI command returned an invalid payload")
        if command.get("intent") != "performance_advice":
            raise AcceptanceError(
                "acceptance command resolved to unexpected intent "
                f"{command.get('intent')!r}"
            )

        actions = command.get("actions")
        if not isinstance(actions, list):
            raise AcceptanceError("Katcha AI command did not return an action list")
        matching = [
            action
            for action in actions
            if isinstance(action, dict) and action.get("type") == ACTION_TYPE
        ]
        if len(matching) != 1:
            raise AcceptanceError(
                "acceptance command must create exactly one "
                f"{ACTION_TYPE} proposal; got {len(matching)}"
            )

        proposal_id = str(matching[0].get("proposal_id") or "")
        if not proposal_id:
            raise AcceptanceError("action proposal did not include proposal_id")

        proposal = _get(
            client,
            f"/v1/ai/actions/{proposal_id}",
            config.aerith_token,
        )
        if not isinstance(proposal, dict):
            raise AcceptanceError("proposal lookup returned an invalid payload")
        if proposal.get("status") != "proposed":
            raise AcceptanceError(
                f"proposal was not awaiting confirmation: {proposal.get('status')!r}"
            )
        if str(proposal.get("channel_profile_id") or "") != str(
            config.channel_profile_id
        ):
            raise AcceptanceError("proposal escaped the requested channel boundary")
        if proposal.get("action_type") != ACTION_TYPE:
            raise AcceptanceError("proposal action type changed before execution")

        execution = _post(
            client,
            f"/v1/ai/actions/{proposal_id}/execute",
            config.aerith_token,
            body={"confirmed": True},
        )
        if not isinstance(execution, dict):
            raise AcceptanceError("action execution returned an invalid payload")
        if execution.get("status") != "executed":
            raise AcceptanceError(
                f"action execution did not reach executed: {execution.get('status')!r}"
            )
        result = execution.get("result") or {}
        workflow_id = str(result.get("workflow_id") or "")
        if not workflow_id:
            raise AcceptanceError("executed action did not return a workflow_id")

        activity = _wait_for_activity(client, config, proposal_id)

        final_proposal = _get(
            client,
            f"/v1/ai/actions/{proposal_id}",
            config.aerith_token,
        )
        if not isinstance(final_proposal, dict):
            raise AcceptanceError("final proposal lookup returned an invalid payload")
        aerith_actor = str(aerith_session.get("actor") or "")
        if final_proposal.get("confirmed_by") != aerith_actor:
            raise AcceptanceError(
                "final proposal was not attributed to the Aerith control actor"
            )

        events, terminal_event_id = _verify_events(
            client,
            config,
            proposal_id,
            aerith_actor,
        )
        cursor = _ack(
            client,
            config,
            terminal_event_id,
            metadata={
                "surface": "control-plane-live-acceptance",
                "phase": "terminal",
                "proposal_id": proposal_id,
            },
        )
        if str(cursor.get("last_event_id") or "") != terminal_event_id:
            raise AcceptanceError(
                "event cursor did not advance to the terminal lifecycle event"
            )

        return {
            "status": "passed",
            "control_contract_version": aerith_session.get(
                "control_contract_version"
            ),
            "katcha_version": aerith_session.get("katcha_version"),
            "operator": {
                "actor": operator_session.get("actor"),
                "credential_id": (operator_session.get("credential") or {}).get(
                    "id"
                ),
            },
            "aerith": {
                "actor": aerith_actor,
                "credential_id": (aerith_session.get("credential") or {}).get(
                    "id"
                ),
                "channel_scoped": not bool(
                    (aerith_session.get("channel_access") or {}).get(
                        "all_channels"
                    )
                ),
            },
            "channel_profile_id": str(config.channel_profile_id),
            "consumer_key": config.consumer_key,
            "baseline_events_acked": baseline_count,
            "ai_live": ai_live,
            "request_id": command.get("request_id"),
            "thread_id": command.get("thread_id"),
            "proposal_id": proposal_id,
            "workflow_id": workflow_id,
            "activity_state": activity.get("state"),
            "lifecycle_events": [
                item.get("event_type")
                for item in events
                if item.get("event_type") in LIFECYCLE_EVENTS
            ],
            "terminal_event_id": terminal_event_id,
            "cursor_last_event_id": cursor.get("last_event_id"),
        }


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise AcceptanceError(f"required environment variable is not set: {name}")
    return value


def _parse_args(argv: list[str]) -> Config:
    parser = argparse.ArgumentParser(
        description="Run Katcha operator/Aerith control-plane live acceptance."
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("KATCHA_ACCEPTANCE_BASE_URL", "http://127.0.0.1:8000"),
    )
    parser.add_argument(
        "--channel-profile-id",
        default=os.getenv("KATCHA_ACCEPTANCE_CHANNEL_PROFILE_ID", ""),
    )
    parser.add_argument(
        "--operator-principal",
        default=os.getenv(
            "KATCHA_ACCEPTANCE_OPERATOR_PRINCIPAL",
            "operator-ui",
        ),
    )
    parser.add_argument(
        "--aerith-principal",
        default=os.getenv("KATCHA_ACCEPTANCE_AERITH_PRINCIPAL", "aerith"),
    )
    parser.add_argument(
        "--consumer-key",
        default=os.getenv("KATCHA_ACCEPTANCE_CONSUMER_KEY", ""),
    )
    parser.add_argument("--request-timeout", type=float, default=60.0)
    parser.add_argument("--settle-timeout", type=float, default=600.0)
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument(
        "--allow-fixture-ai",
        action="store_true",
        help="Diagnostic only: permit the command lifecycle while AI is not live.",
    )
    parser.add_argument(
        "--allow-aerith-all-channels",
        action="store_true",
        help="Explicitly accept a wildcard Aerith channel allowlist.",
    )
    args = parser.parse_args(argv)

    try:
        channel_profile_id = uuid.UUID(str(args.channel_profile_id))
    except ValueError:
        parser.error(
            "--channel-profile-id (or KATCHA_ACCEPTANCE_CHANNEL_PROFILE_ID) "
            "must be a valid UUID"
        )

    consumer_key = str(args.consumer_key).strip() or (
        f"aerith-live-acceptance:{channel_profile_id}"
    )
    if len(consumer_key) > 128:
        parser.error("consumer key must be no more than 128 characters")

    if args.request_timeout <= 0:
        parser.error("--request-timeout must be positive")
    if args.settle_timeout <= 0:
        parser.error("--settle-timeout must be positive")
    if args.poll_seconds <= 0:
        parser.error("--poll-seconds must be positive")

    return Config(
        base_url=str(args.base_url),
        channel_profile_id=channel_profile_id,
        operator_token=_required_env("KATCHA_ACCEPTANCE_OPERATOR_TOKEN"),
        aerith_token=_required_env("KATCHA_ACCEPTANCE_AERITH_TOKEN"),
        operator_principal=str(args.operator_principal),
        aerith_principal=str(args.aerith_principal),
        consumer_key=consumer_key,
        request_timeout=float(args.request_timeout),
        settle_timeout=float(args.settle_timeout),
        poll_seconds=float(args.poll_seconds),
        allow_fixture_ai=bool(args.allow_fixture_ai),
        allow_aerith_all_channels=bool(args.allow_aerith_all_channels),
    )


def main(argv: list[str] | None = None) -> int:
    try:
        config = _parse_args(list(argv or sys.argv[1:]))
        result = run(config)
    except (AcceptanceError, httpx.HTTPError) as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": str(exc),
                },
                indent=2,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
