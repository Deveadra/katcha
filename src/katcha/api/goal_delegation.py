"""An unforgeable, request-scoped identity for *internal* native goal tool calls.

This wrapper is installed only on the in-process ASGITransport after a durable
goal has been revalidated against the current operator configuration. It never
accepts authentication claims from HTTP headers or a persisted goal receipt.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

_SENTINEL = object()


@dataclass(frozen=True)
class DelegatedIdentity:
    actor: str
    principal_name: str
    scopes: frozenset[str]
    channel_id: str
    credential_id: str | None = None
    credential_fingerprint: str | None = None


def delegated_identity(scope: dict) -> DelegatedIdentity | None:
    proof = scope.get("_katcha_native_goal_delegation")
    if (
        isinstance(proof, tuple)
        and len(proof) == 2
        and proof[0] is _SENTINEL
        and isinstance(proof[1], DelegatedIdentity)
    ):
        return proof[1]
    return None


class GoalDelegatedApp:
    def __init__(
        self, app: Any, *, identity: DelegatedIdentity,
        method: str, path: str,
    ) -> None:
        self.app = app
        self.identity = identity
        self.method = method
        self.path = path

    async def __call__(self, scope, receive, send):
        # No delegation for websocket, other paths or other verbs, even if a
        # native route redirects or the ASGI app mounts a different route.
        if (
            scope.get("type") == "http"
            and scope.get("method", "").upper() == self.method
            and scope.get("path") == self.path
        ):
            scope = {**scope, "_katcha_native_goal_delegation": (
                _SENTINEL, self.identity,
            )}
        await self.app(scope, receive, send)
