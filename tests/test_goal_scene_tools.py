"""Native scene tools retain measured/semantic and channel/identity boundaries."""

import asyncio
import uuid
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from katcha.services import goal_tools


@pytest.mark.parametrize("name", [
    "editorial_media_bin", "editorial_scene_index", "index_editorial_media",
    "search_editorial_shots", "editorial_shot_package", "editorial_semantic_match",
])
def test_scene_tool_uses_real_api_schema(name):
    schema = goal_tools.tool_schema(name)
    assert "project_id" in schema["properties"]["path"]["required"]
    assert goal_tools.TOOLS[name].route.startswith("/v1/channels/{channel_profile_id}/")


def test_scene_search_is_read_only_and_index_requires_mutation_authority():
    assert not goal_tools.TOOLS["search_editorial_shots"].mutates
    assert goal_tools.TOOLS["search_editorial_shots"].scope == "ai:read"
    assert goal_tools.TOOLS["index_editorial_media"].mutates
    assert goal_tools.TOOLS["index_editorial_media"].scope == "production:create"
    assert "not character/action" in goal_tools.TOOLS["search_editorial_shots"].description


@pytest.mark.parametrize("key", ["item_id", "item_ids", "package_id"])
def test_scene_ids_must_have_been_observed(monkeypatch, key):
    known = uuid.uuid4()
    monkeypatch.setattr(goal_tools, "_known_ids", lambda goal: {str(known)})
    goal = SimpleNamespace(channel_profile_id=uuid.uuid4())
    def args(value):
        return {"body": {key: [str(value)] if key.endswith("_ids") else str(value)}}
    goal_tools.validate_resource_arguments(goal, args(known))
    with pytest.raises(ValueError, match="was not observed"):
        goal_tools.validate_resource_arguments(goal, args(uuid.uuid4()))


def test_media_item_channel_is_checked_before_native_dispatch(monkeypatch):
    from katcha.api import control_auth
    item_id = uuid.uuid4()
    foreign_channel = uuid.uuid4()
    @contextmanager
    def session_scope():
        yield SimpleNamespace(get=lambda model, identity: SimpleNamespace(
            channel_profile_id=foreign_channel
        ))
    monkeypatch.setattr(goal_tools, "session_scope", session_scope)
    def require_channel(request, channel):
        assert channel == foreign_channel
        raise ValueError("outside channel")
    monkeypatch.setattr(control_auth, "require_control_channel", require_channel)
    with pytest.raises(ValueError, match="outside channel"):
        goal_tools.require_native_resource_channels(
            SimpleNamespace(path_params={"item_id": str(item_id)}),
            goal_tools.TOOLS["editorial_scene_index"],
        )


def test_read_only_post_sends_body_and_returns_observed_results(monkeypatch):
    from fastapi import FastAPI

    from katcha.api import main
    from katcha.editorial.media_intake.scene_schemas import SearchShotsRequest
    project, item, channel = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    schema = goal_tools.tool_schema("search_editorial_shots")
    monkeypatch.setattr(goal_tools, "tool_schema", lambda name: schema)
    monkeypatch.setattr(goal_tools, "resolve_goal_authority", lambda goal: ({"*"}, None))
    monkeypatch.setattr(goal_tools, "_known_ids", lambda goal: {str(project), str(item)})
    monkeypatch.setattr(goal_tools, "require_native_resource_channels", lambda *args: None)
    app = FastAPI()
    captured = []
    @app.post(goal_tools.TOOLS["search_editorial_shots"].route)
    async def search(channel_profile_id: uuid.UUID, project_id: uuid.UUID,
                     body: SearchShotsRequest):
        captured.append((channel_profile_id, project_id, body))
        return [{"item_id": str(item), "shot_id": "measured-shot", "visual_tags": ["dark"]}]
    monkeypatch.setattr(main, "app", app)
    result = asyncio.run(goal_tools.run_native_tool(
        SimpleNamespace(channel_profile_id=channel, actor="local-development"),
        "search_editorial_shots",
        {"path": {"project_id": str(project)},
         "body": {"item_ids": [str(item)], "query": "dark"}},
        uuid.uuid4(),
    ))
    assert captured[0][0:2] == (channel, project)
    assert captured[0][2].item_ids == [item]
    assert result["items"][0]["shot_id"] == "measured-shot"


def test_http_tools_reject_flat_filters_instead_of_silently_ignoring_them(monkeypatch):
    monkeypatch.setattr(goal_tools, "resolve_goal_authority", lambda goal: ({"*"}, None))
    monkeypatch.setattr(goal_tools, "validate_resource_arguments", lambda *args: None)
    with pytest.raises(ValueError, match="unsupported top-level arguments: q.*inspect_tool"):
        asyncio.run(goal_tools.run_native_tool(
            SimpleNamespace(channel_profile_id=uuid.uuid4()), "clip_library",
            {"q": "Kate Bishop coin"}, uuid.uuid4(),
        ))
