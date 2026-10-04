"""Shared typed native catalog. No model-supplied URLs, HTTP methods or credentials."""

from __future__ import annotations

import asyncio
import re
import uuid
from contextlib import suppress
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import select

from katcha.acquisition_models import DiscoveryCandidate, IngestionSource
from katcha.ai.command_planner import ClipLookup
from katcha.db import session_scope
from katcha.goal_models import CommandGoal
from katcha.services.command_actions import ActionProposalSpec
from katcha.services.command_environment import command_environment
from katcha.services.goal_receipts import resolve_goal_authority


@dataclass(frozen=True)
class GoalTool:
    name: str
    description: str
    scope: str
    method: str = "CUSTOM"
    route: str = ""
    action_type: str | None = None
    confirm: bool = False
    retry_safe: bool = False

    @property
    def mutates(self) -> bool:
        return self.action_type is not None or self.method not in {"GET", "CUSTOM"}


TOOLS = {
    tool.name: tool
    for tool in [
        GoalTool(
            "editorial_narration", "List measured operator recordings for a saved script revision; "
            "use returned recording IDs in a narrated storyboard. Does not generate speech.",
            "ai:read", "GET",
            "/v1/channels/{channel_profile_id}/editorial-projects/{project_id}/narration",
        ),
        GoalTool(
            "editorial_render_review", "Read current render approval and any invalidation reason",
            "ai:read", "GET",
            "/v1/channels/{channel_profile_id}/editorial-projects/{project_id}"
            "/runs/{editorial_run_id}/review",
        ),
        GoalTool(
            "review_editorial_render",
            "Record an explicit operator review of this exact render. Approval rechecks current "
            "script, media and clearance. Requires operator confirmation; does not publish.",
            "production:create", "POST",
            "/v1/channels/{channel_profile_id}/editorial-projects/{project_id}"
            "/runs/{editorial_run_id}/review", confirm=True, retry_safe=True,
        ),
        GoalTool(
            "preflight_editorial_storyboard",
            "Validate an explicit caption-only storyboard against the current script and "
            "acquired media, source timing, evidence references and current rights. "
            "Returns a deterministic manifest; does not render or approve publication.",
            "production:create", "POST",
            "/v1/channels/{channel_profile_id}/editorial-projects/{project_id}/storyboard/preflight",
            retry_safe=True,
        ),
        GoalTool(
            "start_editorial_analysis",
            "Start durable editorial work: target=analysis prepares sampled frames/transcript; "
            "target=script interprets sources, researches evidence and drafts a cited script. "
            "target=assets scouts supporting media; target=acquire_assets downloads selected "
            "scout candidates for review. target=render requires an asset_run_id and explicit "
            "captioned_silent or narrated storyboard. Narrated requires uploaded recording IDs for "
            "every beat; uses local rendering with a private review preview. "
            "Research requires a live "
            "eligible provider; discovery and generation are not rights/publication approval.",
            "production:create", "POST",
            "/v1/channels/{channel_profile_id}/editorial-projects/{project_id}/runs",
            retry_safe=True,
        ),
        GoalTool(
            "editorial_runs", "List editorial execution progress and saved blockers",
            "ai:read", "GET",
            "/v1/channels/{channel_profile_id}/editorial-projects/{project_id}/runs",
        ),
        GoalTool(
            "editorial_run", "Inspect exact editorial receipts, source coverage and progress",
            "ai:read", "GET",
            "/v1/channels/{channel_profile_id}/editorial-projects/{project_id}"
            "/runs/{editorial_run_id}",
        ),
        GoalTool(
            "resume_editorial_run", "Resume failed work from saved checkpoints",
            "production:create", "POST",
            "/v1/channels/{channel_profile_id}/editorial-projects/{project_id}"
            "/runs/{editorial_run_id}/resume", retry_safe=True,
        ),
        GoalTool(
            "cancel_editorial_run", "Stop further editorial stages; retain saved work",
            "production:create", "POST",
            "/v1/channels/{channel_profile_id}/editorial-projects/{project_id}"
            "/runs/{editorial_run_id}/cancel", retry_safe=True,
        ),
        GoalTool(
            "create_editorial_project",
            "Save a researched-original brief and source URLs. Draft storage only; "
            "does not start analysis, research, generation or rendering.",
            "production:create", "POST",
            "/v1/channels/{channel_profile_id}/editorial-projects",
            retry_safe=True,
        ),
        GoalTool(
            "list_editorial_projects", "Browse saved original-episode drafts in this channel",
            "ai:read", "GET", "/v1/channels/{channel_profile_id}/editorial-projects",
        ),
        GoalTool(
            "editorial_project", "Read a saved editorial project's brief and capabilities",
            "ai:read", "GET",
            "/v1/channels/{channel_profile_id}/editorial-projects/{project_id}",
        ),
        GoalTool(
            "editorial_revision", "Read one immutable saved script and its evidence",
            "ai:read", "GET",
            "/v1/channels/{channel_profile_id}/editorial-projects/{project_id}/revisions/{revision}",
        ),
        GoalTool(
            "editorial_revisions", "Read saved evidence and script revisions; latest first",
            "ai:read", "GET",
            "/v1/channels/{channel_profile_id}/editorial-projects/{project_id}/revisions",
        ),
        GoalTool(
            "save_editorial_draft",
            "Save structurally validated evidence/script with expected_revision. "
            "Does not independently verify claims, approve rights or publish.",
            "production:create", "POST",
            "/v1/channels/{channel_profile_id}/editorial-projects/{project_id}/revisions",
            retry_safe=True,
        ),
        GoalTool(
            "inspect_tool", "Get exact argument schema for a registered capability", "ai:read"
        ),
        GoalTool(
            "channel_state",
            "Current identity, interests, formats, source and recipe identities",
            "ai:read",
        ),
        GoalTool(
            "find_clips",
            "Search scored channel clips using semantic topic/date arguments",
            "ai:read",
        ),
        GoalTool(
            "list_goals",
            "Read saved goals and their current status",
            "ai:read",
            "GET",
            "/v1/ai/goals",
        ),
        GoalTool(
            "cancel_goal",
            "Stop planning an exact saved goal; started work keeps its status",
            "ai:write",
            "POST",
            "/v1/ai/goals/{goal_id}/cancel",
            retry_safe=True,
        ),
        GoalTool(
            "workflow_status", "Inspect an observed workflow's actual execution state", "ai:read"
        ),
        GoalTool(
            "cancel_workflow",
            "Request cancellation of an exact observed workflow",
            "workflows:cancel",
            action_type="native_tool",
            confirm=True,
            retry_safe=True,
        ),
        GoalTool("research", "Retained research and current trend evidence", "ai:read"),
        GoalTool("failures", "Inspect current channel failures and recovery targets", "ai:read"),
        GoalTool(
            "search_source",
            "Search a resolved saved source and optionally ingest/analyze matches",
            "discovery:write",
            action_type="start_source_scout",
            retry_safe=True,
        ),
        GoalTool(
            "scout_web",
            "One bounded search beyond saved sources; optionally ingest/analyze matches",
            "discovery:write",
            action_type="start_source_scout",
            retry_safe=True,
        ),
        GoalTool(
            "make_short",
            "Create production from one observed channel clip and recipe",
            "production:create",
            action_type="create_short_production",
            retry_safe=True,
        ),
        GoalTool(
            "make_ranking",
            "Create ranked production from exact observed clips in order",
            "production:create",
            action_type="create_ranked_short_episode",
            retry_safe=True,
        ),
        GoalTool(
            "refresh_intelligence",
            "Refresh channel learning and performance",
            "intelligence:write",
            action_type="refresh_channel_intelligence",
            retry_safe=True,
        ),
        GoalTool(
            "recover_render",
            "Recover one observed dead-letter production render",
            "render:recover",
            action_type="recover_production_render",
            retry_safe=True,
        ),
        GoalTool(
            "list_sources",
            "Browse enabled or disabled saved channel sources",
            "ai:read",
            "GET",
            "/v1/discovery/sources",
        ),
        GoalTool(
            "source_library",
            "Search and page saved sources by name, topic or platform",
            "ai:read",
            "GET",
            "/v1/discovery/source-library",
        ),
        GoalTool(
            "discovery_adapters",
            "Actual adapter capabilities, query fields and credential requirements",
            "ai:read",
            "GET",
            "/v1/discovery/adapters",
        ),
        GoalTool(
            "save_source",
            "Create/update/enable/disable a source in this channel",
            "discovery:write",
            "POST",
            "/v1/discovery/sources",
        ),
        GoalTool(
            "source_history",
            "Inspect source runs and real failures",
            "ai:read",
            "GET",
            "/v1/discovery/sources/{source_id}/runs",
        ),
        GoalTool(
            "source_results",
            "Inspect candidates returned by a specific source run",
            "ai:read",
            "GET",
            "/v1/discovery/sources/{source_id}/runs/{run_id}/results",
        ),
        GoalTool(
            "clip_library",
            "Paginated channel media library; refine q/offset rather than assuming an empty pool",
            "ai:read",
            "GET",
            "/v1/clips/library",
        ),
        GoalTool(
            "trend_profile",
            "Read current trend interests and filters",
            "trends:read",
            "GET",
            "/v1/channels/{channel_profile_id}/trends/watch-profile",
        ),
        GoalTool(
            "save_trend_profile",
            "Save trend interests, entities, platforms and exclusions",
            "trends:write",
            "POST",
            "/v1/channels/{channel_profile_id}/trends/watch-profile",
        ),
        GoalTool(
            "list_watches",
            "Read ongoing topic searches",
            "trends:read",
            "GET",
            "/v1/channels/{channel_profile_id}/trends/watches",
        ),
        GoalTool(
            "save_watch",
            "Create a version of an ongoing discovery topic watch",
            "trends:write",
            "POST",
            "/v1/channels/{channel_profile_id}/trends/watches",
        ),
        GoalTool(
            "run_watch",
            "Run an exact channel topic watch once",
            "trends:write",
            "POST",
            "/v1/trends/watches/{topic_watch_id}/execute",
            retry_safe=True,
        ),
        GoalTool(
            "schedule_watch",
            "Start continuing collection for an exact channel topic watch",
            "trends:write",
            "POST",
            "/v1/trends/watches/{topic_watch_id}/schedule",
            retry_safe=True,
        ),
        GoalTool(
            "watch_results",
            "Read ranked candidates from an exact channel watch",
            "trends:read",
            "GET",
            "/v1/trends/watches/{topic_watch_id}/ranked",
        ),
        GoalTool(
            "trend_opportunities",
            "Read current channel trend opportunities",
            "trends:read",
            "GET",
            "/v1/channels/{channel_profile_id}/trends/opportunities",
        ),
        GoalTool(
            "channel_details",
            "Read native channel strategy, automation and identity",
            "channels:read",
            "GET",
            "/v1/channels/{channel_profile_id}",
        ),
        GoalTool(
            "save_strategy",
            "Update channel strategy and budget through native validation",
            "intelligence:write",
            "POST",
            "/v1/channels/{channel_profile_id}/strategy",
        ),
        GoalTool(
            "retention",
            "Read channel clip retention settings",
            "channels:read",
            "GET",
            "/v1/channels/{channel_profile_id}/clip-retention",
        ),
        GoalTool(
            "save_retention",
            "Save retention settings; this does not delete media",
            "channels:write",
            "PUT",
            "/v1/channels/{channel_profile_id}/clip-retention",
        ),
        GoalTool(
            "edit_templates",
            "Read supported native editing templates",
            "channels:read",
            "GET",
            "/v1/channels/edit-blueprint-templates",
        ),
        GoalTool(
            "edit_recipes",
            "Read full active/staged channel editing recipes",
            "channels:read",
            "GET",
            "/v1/channels/{channel_profile_id}/edit-blueprints",
        ),
        GoalTool(
            "save_edit_recipe",
            "Stage a validated editing recipe",
            "channels:write",
            "POST",
            "/v1/channels/{channel_profile_id}/edit-blueprints",
        ),
        GoalTool(
            "activate_edit_recipe",
            "Activate an exact staged recipe version",
            "channels:write",
            "POST",
            "/v1/channels/{channel_profile_id}/edit-blueprints/{blueprint_key}/{version}/activate",
        ),
        GoalTool(
            "brands",
            "Read channel branding and voice contract",
            "channels:read",
            "GET",
            "/v1/channels/{channel_profile_id}/brands",
        ),
        GoalTool(
            "voice_settings",
            "Read configured voice IDs and enabled state",
            "channels:read",
            "GET",
            "/v1/integrations/elevenlabs/channels/{channel_profile_id}",
        ),
        GoalTool(
            "voice_catalog",
            "Inspect available provider voice IDs",
            "channels:read",
            "GET",
            "/v1/integrations/elevenlabs/voices",
        ),
        GoalTool(
            "save_voice",
            "Save channel voice IDs through native validation",
            "channels:write",
            "PUT",
            "/v1/integrations/elevenlabs/channels/{channel_profile_id}",
        ),
        GoalTool(
            "enable_voice",
            "Enable or disable channel narration",
            "channels:write",
            "PATCH",
            "/v1/integrations/elevenlabs/channels/{channel_profile_id}/enabled",
        ),
        GoalTool(
            "confirm_proposal",
            "Authorize an exact existing frozen proposal; never change its arguments",
            "ai:write",
            "POST",
            "/v1/ai/actions/{proposal_id}/execute",
            retry_safe=True,
        ),
        GoalTool(
            "brand_candidates",
            "Read supported brand contracts",
            "channels:read",
            "GET",
            "/v1/channels/{channel_profile_id}/brand-candidates",
        ),
        GoalTool(
            "stage_brand",
            "Stage a brand/voice contract; does not activate it",
            "channels:write",
            "POST",
            "/v1/channels/{channel_profile_id}/brands",
        ),
        GoalTool(
            "activate_brand",
            "Activate an exact staged brand/voice contract version",
            "channels:write",
            "POST",
            "/v1/channels/{channel_profile_id}/brands/{version}/activate",
        ),
        GoalTool(
            "production",
            "Read exact production status and review evidence",
            "ai:read",
            "GET",
            "/v1/productions/{production_id}",
        ),
        GoalTool(
            "episode",
            "Read exact ranked episode status and review evidence",
            "ai:read",
            "GET",
            "/v1/short-episodes/{short_episode_id}",
        ),
        GoalTool(
            "review_production",
            "Review exact production; always requires operator confirmation",
            "production:create",
            "POST",
            "/v1/productions/{production_id}/review",
            confirm=True,
        ),
        GoalTool(
            "review_episode",
            "Review exact episode; always requires operator confirmation",
            "production:create",
            "POST",
            "/v1/short-episodes/{short_episode_id}/review",
            confirm=True,
        ),
        GoalTool(
            "publish_production",
            "Publish reviewed production with native rights/approval gates; operator "
            "confirmation required",
            "production:create",
            "POST",
            "/v1/productions/{production_id}/publications",
            confirm=True,
        ),
        GoalTool(
            "publish_episode",
            "Publish reviewed ranked episode with native gates; operator confirmation required",
            "production:create",
            "POST",
            "/v1/short-episodes/{short_episode_id}/publications",
            confirm=True,
        ),
        GoalTool(
            "publication",
            "Read a channel publication and provider state",
            "ai:read",
            "GET",
            "/v1/publications/{publication_id}",
        ),
        GoalTool(
            "analytics",
            "Read stored publication analytics",
            "ai:read",
            "GET",
            "/v1/publications/{publication_id}/analytics",
        ),
        GoalTool(
            "refresh_analytics",
            "Synchronize real publication analytics",
            "intelligence:write",
            "POST",
            "/v1/publications/{publication_id}/analytics/refresh",
        ),
    ]
}


class SourceSearch(BaseModel):
    source_id: uuid.UUID
    query: str = Field(min_length=1, max_length=320)
    prepare: bool = False
    media_kind: str = Field(default="any", pattern="^(any|trailer|teaser)$")


class WebScout(BaseModel):
    query: str = Field(min_length=1, max_length=320)
    platforms: list[Literal["youtube", "tiktok", "instagram", "x", "bluesky"]] = Field(
        default_factory=list, max_length=8
    )
    prepare: bool = False
    media_kind: str = Field(default="any", pattern="^(any|trailer|teaser)$")


class ProductionSelection(BaseModel):
    clip_ids: list[uuid.UUID] = Field(min_length=1, max_length=7)
    premise: str = Field(default="", max_length=500)
    edit_blueprint_key: str | None = None


def tool_catalog(scopes: set[str]) -> list[dict]:
    return [
        {
            "name": t.name,
            "description": t.description,
            "mutates": t.mutates,
            "requires_confirmation": t.confirm,
            "allowed": "*" in scopes or t.scope in scopes,
        }
        for t in TOOLS.values()
    ]


def _expand_schema(value: Any, components: dict, depth: int = 0) -> Any:
    if depth > 12:
        return {
            "description": "Nested native configuration; inspect current configuration "
            "before editing"
        }
    if isinstance(value, dict):
        if "$ref" in value:
            return _expand_schema(components[value["$ref"].split("/")[-1]], components, depth + 1)
        return {k: _expand_schema(v, components, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_schema(v, components, depth + 1) for v in value]
    return value


def tool_schema(name: str) -> dict:
    tool = TOOLS[name]
    if tool.method == "CUSTOM":
        schemas = {
            "find_clips": ClipLookup,
            "search_source": SourceSearch,
            "scout_web": WebScout,
            "make_short": ProductionSelection,
            "make_ranking": ProductionSelection,
        }
        if name in schemas:
            return schemas[name].model_json_schema()
        if name == "inspect_tool":
            return {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            }
        if name == "research":
            return {
                "type": "object",
                "properties": {
                    "terms": {"type": "array", "items": {"type": "string"}},
                    "offset": {"type": "integer", "minimum": 0},
                },
            }
        if name in {"workflow_status", "cancel_workflow"}:
            return {
                "type": "object",
                "properties": {"workflow_id": {"type": "string", "minLength": 1, "maxLength": 255}},
                "required": ["workflow_id"],
            }
        if name == "recover_render":
            return {
                "type": "object",
                "properties": {"production_id": {"type": "string", "format": "uuid"}},
                "required": ["production_id"],
            }
        return {"type": "object", "properties": {}}
    from katcha.api.main import app

    api = app.openapi()
    operation = api["paths"][tool.route][tool.method.lower()]
    result = {
        "type": "object",
        "properties": {
            "offset": {"type": "integer", "minimum": 0, "maximum": 10000},
            "path": {"type": "object", "properties": {}},
            "query": {"type": "object", "properties": {}},
            "body": {"type": "object"},
        },
    }
    for parameter in operation.get("parameters", []):
        if parameter["in"] in {"path", "query"}:
            container = result["properties"][parameter["in"]]
            container["properties"][parameter["name"]] = parameter["schema"]
            if parameter.get("required"):
                container.setdefault("required", []).append(parameter["name"])
    body = (
        operation.get("requestBody", {})
        .get("content", {})
        .get("application/json", {})
        .get("schema")
    )
    if body:
        result["properties"]["body"] = body
    return _expand_schema(result, api.get("components", {}).get("schemas", {}))


def _known_ids(goal: CommandGoal) -> set[str]:
    from katcha.services.command_history import list_command_turns, list_thread_proposals

    result: set[str] = set()

    def visit(value):
        if isinstance(value, str):
            with suppress(ValueError):
                result.add(str(uuid.UUID(value)))
        elif isinstance(value, dict):
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(goal.request.get("selected_clip_ids", []))
    visit(goal.request.get("resource_refs", []))
    visit(goal.request.get("selected_production_id"))
    visit(goal.observations)
    visit(command_environment(goal.channel_profile_id))
    for turn in list_command_turns(goal.thread_id)[-12:]:
        visit(turn.evidence)
    for proposal in list_thread_proposals(goal.thread_id)[-20:]:
        visit(str(proposal.id))
        visit(proposal.result)
    return result


def validate_resource_arguments(goal: CommandGoal, arguments: dict) -> None:
    known = _known_ids(goal)

    def visit(value, key=""):
        if isinstance(value, dict):
            for name, item in value.items():
                visit(item, name)
        elif isinstance(value, list):
            for item in value:
                visit(item, key[:-1] if key.endswith("_ids") else key)
        elif key == "channel_profile_id":
            if value and str(value) != str(goal.channel_profile_id):
                raise ValueError("The tool attempted to use a different channel")
        elif (
            key
            in {
                "source_id",
                "clip_id",
                "production_id",
                "short_episode_id",
                "publication_id",
                "run_id",
                "candidate_id",
                "preview_id",
                "proposal_id",
                "goal_id",
                "topic_watch_id",
                "project_id",
                "editorial_run_id",
                "scout_run_id",
                "asset_run_id",
            }
            and value is not None
        ):
            try:
                normalized = str(uuid.UUID(str(value)))
            except ValueError:
                raise ValueError(f"{key} must be an observed resource identity") from None
            if normalized not in known:
                raise ValueError(f"{key} was not observed in this goal or conversation")

    visit(arguments)


def action_spec(goal: CommandGoal, name: str, args: dict, step_id: uuid.UUID) -> ActionProposalSpec:
    tool = TOOLS[name]
    validate_resource_arguments(goal, args)
    if name == "cancel_workflow":
        observed_workflow_id(goal, args)
    if name == "search_source":
        selection = SourceSearch.model_validate(args)
        with session_scope() as session:
            source = session.get(IngestionSource, selection.source_id)
            if source is None or not source.enabled:
                raise ValueError("The selected source is unavailable")
            if source.channel_profile_id not in {None, goal.channel_profile_id}:
                raise ValueError("The selected source belongs to another channel")
            query = {
                "q": selection.query,
                "order": "relevance",
                "freshness_horizon_hours": 0,
                "limit": 25,
            }
            reference = (source.query_template or {}).get("channel_reference")
            if reference:
                query["channel_reference"] = reference
            payload = {
                "source_id": str(source.id),
                "source_name": source.name,
                "search_query": selection.query,
                "query_overrides": query,
                "prepare_for_production": selection.prepare,
                "media_kind": selection.media_kind,
                "operator_request": goal.request["prompt"],
            }
    elif name == "scout_web":
        selection = WebScout.model_validate(args)
        payload = {
            "terms": [selection.query],
            "platforms": selection.platforms,
            "recurring": False,
            "prepare_for_production": selection.prepare,
            "media_kind": selection.media_kind,
            "operator_request": goal.request["prompt"],
        }
    elif name in {"make_short", "make_ranking"}:
        selection = ProductionSelection.model_validate(args)
        if name == "make_short" and len(selection.clip_ids) != 1:
            raise ValueError("A short requires exactly one observed clip")
        payload = (
            {"clip_id": str(selection.clip_ids[0])}
            if name == "make_short"
            else {
                "clip_ids": [str(value) for value in selection.clip_ids],
                "premise": selection.premise,
                "item_count": len(selection.clip_ids),
                "preserve_candidate_order": True,
            }
        )
        if selection.edit_blueprint_key:
            payload["edit_blueprint_key"] = selection.edit_blueprint_key
    elif name == "recover_render":
        payload = {"production_id": str(uuid.UUID(args["production_id"]))}
    elif name == "refresh_intelligence":
        payload = {}
    else:
        payload = {
            "goal_id": str(goal.id),
            "step_id": str(step_id),
            "tool": name,
            "arguments": args,
        }
    return ActionProposalSpec(
        tool.action_type or "native_tool",
        name.replace("_", " ").capitalize(),
        tool.description,
        payload,
    )


def _redact(value):
    if isinstance(value, dict):
        result = {
            k: _redact(v)
            for k, v in value.items()
            if not any(
                part in k.casefold()
                for part in (
                    "token",
                    "secret",
                    "password",
                    "api_key",
                    "authorization",
                    "cookie",
                    "credential",
                )
            )
        }
        truncated = [
            k
            for k, v in value.items()
            if (isinstance(v, list) and len(v) > 50) or (isinstance(v, str) and len(v) > 4000)
        ]
        if truncated:
            result["_truncated_fields"] = truncated
        return result
    if isinstance(value, list):
        return [_redact(item) for item in value[:50]]
    if isinstance(value, str):
        return value[:4000]
    return value


async def run_read_tool(goal: CommandGoal, name: str, args: dict) -> dict:
    if name == "workflow_status":
        from katcha.orchestration.client import get_temporal_client

        workflow_id = observed_workflow_id(goal, args)
        client = await get_temporal_client()
        description = await client.get_workflow_handle(workflow_id).describe()
        return {"workflow_id": workflow_id, "workflow_state": description.status.name}
    if name == "inspect_tool":
        return {"name": args["name"], "schema": tool_schema(args["name"])}
    if name == "channel_state":
        return command_environment(goal.channel_profile_id)
    if name == "find_clips":
        from katcha.services.command_center import best_clips

        lookup = ClipLookup.model_validate(args)
        summary, evidence = best_clips(
            goal.channel_profile_id, goal.request["prompt"], lookup=lookup
        )
        return {
            "summary": summary,
            "evidence": evidence,
            "next_pool_offset": lookup.pool_offset + 250,
            "pool_limit": 250,
        }
    if name == "research":
        from katcha.services.command_resources import research_context

        summary, evidence = research_context(
            goal.channel_profile_id,
            args.get("terms", []),
            offset=max(0, min(int(args.get("offset", 0)), 10000)),
        )
        return {"summary": summary, "evidence": evidence, "next_offset": args.get("offset", 0) + 4}
    if name == "failures":
        from katcha.services.command_center import failures

        summary, evidence = failures(goal.channel_profile_id)
        return {"summary": summary, "evidence": evidence}
    return await run_native_tool(goal, name, args, uuid.uuid5(goal.id, f"read:{goal.step_count}"))


async def run_native_tool(goal: CommandGoal, name: str, args: dict, step_id: uuid.UUID) -> dict:
    tool = TOOLS[name]
    scopes, token = resolve_goal_authority(goal)
    if "*" not in scopes and tool.scope not in scopes:
        raise ValueError(f"This request no longer has {tool.scope} permission")
    validate_resource_arguments(goal, args)
    if name == "cancel_workflow":
        from katcha.orchestration.client import get_temporal_client

        workflow_id = observed_workflow_id(goal, args)
        client = await get_temporal_client()
        await client.get_workflow_handle(workflow_id).cancel()
        return {"workflow_id": workflow_id, "cancellation_requested": True}
    path_args, query, body = (
        dict(args.get("path", {})),
        dict(args.get("query", {})),
        dict(args.get("body", {})),
    )
    path_args["channel_profile_id"] = str(goal.channel_profile_id)
    require_native_resource_channels(
        SimpleNamespace(
            path_params=path_args,
            state=SimpleNamespace(control_channel_profile_ids={str(goal.channel_profile_id)}),
        ),
        tool,
    )
    schema = tool_schema(name)
    if "channel_profile_id" in schema["properties"]["query"]["properties"]:
        query["channel_profile_id"] = str(goal.channel_profile_id)
    properties = schema["properties"]["body"].get("properties", {})
    if "channel_profile_id" in properties:
        body["channel_profile_id"] = str(goal.channel_profile_id)
    if "actor" in properties:
        body["actor"] = goal.actor
    if name == "confirm_proposal":
        body["confirmed"] = True
    if "idempotency_key" in properties:
        body["idempotency_key"] = f"goal-step:{step_id}"
    if name == "save_source":
        with session_scope() as session:
            source = session.scalar(
                select(IngestionSource).where(IngestionSource.source_key == body.get("source_key"))
            )
            if source and source.channel_profile_id != goal.channel_profile_id:
                raise ValueError("A channel goal cannot replace a shared or other channel source")
    if name == "save_watch":
        for entry in body.get("source_queries", []):
            if isinstance(entry, dict) and entry.get("channel_profile_id") not in {
                None,
                str(goal.channel_profile_id),
            }:
                raise ValueError("A topic watch source belongs to a different channel")
    from katcha.api.main import app

    url = tool.route
    for key in re.findall(r"\{([^}]+)\}", url):
        value = str(path_args[key])
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
            raise ValueError("Invalid native resource path")
        url = url.replace("{" + key + "}", value)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://katcha-native"
    ) as client:
        response = await asyncio.wait_for(
            client.request(
                tool.method,
                url,
                params=query,
                json=body if tool.mutates else None,
                headers=headers,
                timeout=90,
            ),
            timeout=90,
        )
    if response.is_error:
        raise ValueError(
            f"Native {name} rejected the request (HTTP {response.status_code}): "
            f"{response.text[:1000]}"
        )
    payload = response.json()
    if isinstance(payload, list):
        offset = max(0, min(int(args.get("offset", 0)), 10000))
        payload = {
            "items": payload[offset : offset + 20],
            "total": len(payload),
            "next_offset": offset + 20,
        }
    return _redact(payload)


def native_route_tool(path: str, method: str) -> GoalTool | None:
    for tool in TOOLS.values():
        if tool.method != method:
            continue
        pattern = re.sub(r"\{[^}]+\}", "[^/]+", tool.route)
        if re.fullmatch(pattern, path):
            return tool
    return None


def require_native_resource_channels(request, tool: GoalTool) -> None:
    """Also guard direct scoped API access, not only goal callers."""
    from katcha.acquisition_models import TopicWatchVersion
    from katcha.api.control_auth import require_control_channel
    from katcha.editorial_models import EditorialProject, EditorialRun
    from katcha.intelligence_models import ChannelProfile
    from katcha.models import Clip
    from katcha.production_models import Production
    from katcha.publishing_models import Publication
    from katcha.services.clip_lifecycle import channel_ids_for_clip
    from katcha.short_episode_models import ShortEpisode

    mapping = {
        "project_id": EditorialProject,
        "editorial_run_id": EditorialRun,
        "scout_run_id": EditorialRun,
        "asset_run_id": EditorialRun,
        "source_id": IngestionSource,
        "production_id": Production,
        "short_episode_id": ShortEpisode,
        "publication_id": Publication,
        "candidate_id": DiscoveryCandidate,
        "clip_id": Clip,
        "topic_watch_id": TopicWatchVersion,
    }
    with session_scope() as session:
        for key, model in mapping.items():
            if key not in request.path_params:
                continue
            row = session.get(model, uuid.UUID(str(request.path_params[key])))
            if row is None:
                continue
            if model is Publication:
                channels = list(
                    session.scalars(
                        select(ChannelProfile.id).where(
                            ChannelProfile.youtube_connection_id == row.youtube_connection_id
                        )
                    )
                )
                if not channels:
                    raise ValueError("Publication is not assigned to a channel")
                for channel in channels:
                    try:
                        require_control_channel(request, channel)
                        break
                    except Exception:
                        continue
                else:
                    raise ValueError("Publication is outside the principal's channels")
            elif model is Clip:
                channels = channel_ids_for_clip(session, row.id)
                if not channels:
                    raise ValueError("Clip is not assigned to a channel")
                for channel in channels:
                    try:
                        require_control_channel(request, channel)
                        break
                    except Exception:
                        continue
                else:
                    raise ValueError("Clip is outside the principal's channels")
            elif getattr(row, "channel_profile_id", None):
                require_control_channel(request, row.channel_profile_id)
            else:
                raise ValueError("Resource does not have channel ownership for this operation")


def observed_workflow_id(goal: CommandGoal, args: dict) -> str:
    from katcha.services.command_history import list_thread_proposals

    requested = args.get("workflow_id")
    known = set()

    def visit(value):
        if isinstance(value, dict):
            if isinstance(value.get("workflow_id"), str):
                known.add(value["workflow_id"])
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(goal.observations)
    for proposal in list_thread_proposals(goal.thread_id):
        visit(proposal.result)
    if not requested or requested not in known:
        raise ValueError("The workflow was not observed in this goal or channel conversation")
    return requested
