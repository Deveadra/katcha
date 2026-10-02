from __future__ import annotations

from dataclasses import dataclass

import httpx

from katcha.config import Settings, get_settings


class LeadershipFenceError(RuntimeError):
    """Raised when this runtime cannot prove authority for an external mutation."""


@dataclass(frozen=True, slots=True)
class LeadershipReceipt:
    deployment_id: str
    deployment_epoch: int
    active_epoch: int
    leader_id: str


def assert_mutation_authority(
    operation: str,
    *,
    settings: Settings | None = None,
) -> LeadershipReceipt | None:
    resolved = settings or get_settings()
    if resolved.leadership_fence_mode == "disabled":
        return None

    if resolved.leadership_fence_mode != "http":
        raise LeadershipFenceError(
            f"unsupported leadership fence mode: {resolved.leadership_fence_mode}"
        )
    endpoint = (resolved.leadership_fence_url or "").strip()
    token = (
        resolved.leadership_fence_token.get_secret_value().strip()
        if resolved.leadership_fence_token is not None
        else ""
    )
    deployment_id = resolved.deployment_id.strip()
    deployment_epoch = int(resolved.deployment_epoch)
    if not endpoint or not token or not deployment_id or deployment_epoch < 1:
        raise LeadershipFenceError(
            "leadership fencing is enabled but the runtime identity is incomplete"
        )

    try:
        response = httpx.post(
            endpoint,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={
                "deployment_id": deployment_id,
                "deployment_epoch": deployment_epoch,
                "operation": operation,
            },
            timeout=resolved.leadership_fence_timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise LeadershipFenceError(
            f"leadership coordinator unavailable for {operation}"
        ) from exc

    if not isinstance(payload, dict):
        raise LeadershipFenceError(
            f"leadership coordinator returned an invalid response for {operation}"
        )
    try:
        active_epoch = int(payload["active_epoch"])
        leader_id = str(payload["leader_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise LeadershipFenceError(
            f"leadership coordinator omitted authority state for {operation}"
        ) from exc

    authorized = payload.get("authorized") is True
    if (
        not authorized
        or active_epoch != deployment_epoch
        or leader_id != deployment_id
    ):
        raise LeadershipFenceError(
            "runtime is fenced from external mutation "
            f"{operation}: local={deployment_id}@{deployment_epoch} "
            f"active={leader_id}@{active_epoch}"
        )

    return LeadershipReceipt(
        deployment_id=deployment_id,
        deployment_epoch=deployment_epoch,
        active_epoch=active_epoch,
        leader_id=leader_id,
    )
