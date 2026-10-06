from __future__ import annotations

CONTROL_CONTRACT_VERSION = "1"

CONTROL_CAPABILITY_SCOPES: dict[str, str] = {
    "ai_read": "ai:read",
    "ai_command": "ai:command",
    "ai_write": "ai:write",
    "channels_read": "channels:read",
    "channels_write": "channels:write",
    "intelligence_write": "intelligence:write",
    "production_create": "production:create",
    "render_recover": "render:recover",
    "discovery_write": "discovery:write",
    "events_read": "events:read",
    "events_ack": "events:ack",
    "trends_read": "trends:read",
    "trends_write": "trends:write",
}

COMMAND_ACTION_SCOPES: dict[str, str] = {
    "native_tool": "ai:write",
    "refresh_channel_intelligence": "intelligence:write",
    "create_short_production": "production:create",
    "create_ranked_short_episode": "production:create",
    "recover_production_render": "render:recover",
    "start_source_scout": "discovery:write",
    "editorial_operation": "production:create",
}


def allows_scope(scopes: set[str], scope: str) -> bool:
    return "*" in scopes or scope in scopes


def capability_flags(scopes: set[str]) -> dict[str, bool]:
    return {
        name: allows_scope(scopes, scope)
        for name, scope in CONTROL_CAPABILITY_SCOPES.items()
    }


def command_action_permissions(
    scopes: set[str],
) -> dict[str, dict[str, object]]:
    return {
        action_type: {
            "required_scope": required_scope,
            "allowed": allows_scope(scopes, required_scope),
        }
        for action_type, required_scope in COMMAND_ACTION_SCOPES.items()
    }
