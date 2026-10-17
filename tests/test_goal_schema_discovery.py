import asyncio
from types import SimpleNamespace

import pytest

from katcha.services import goal_tools


def test_planner_can_discover_the_discovery_tools_argument_contract():
    catalog = goal_tools.tool_catalog({"ai:read"})
    discovery = next(tool for tool in catalog if tool["name"] == "inspect_tool")
    assert discovery["allowed"] is True
    assert discovery["argument_schema"]["required"] == ["name"]
    assert discovery["argument_schema"]["properties"]["name"]["type"] == "string"
    assert next(tool for tool in catalog if tool["name"] == "make_short")["allowed"] is False


@pytest.mark.parametrize("field", ["name", "tool_name"])
def test_observed_planner_argument_spellings_resolve_to_the_same_capability(field):
    result = asyncio.run(
        goal_tools.run_read_tool(SimpleNamespace(), "inspect_tool", {field: "production"})
    )
    assert result["name"] == "production"
    assert "production_id" in result["schema"]["properties"]["path"]["properties"]


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"name": None},
        {"name": "unknown-capability"},
        {"name": "production", "tool_name": "make_short"},
        {"name": "production", "unexpected": True},
    ],
)
def test_invalid_discovery_arguments_fail_before_schema_resolution(monkeypatch, arguments):
    def unexpected_schema(*args):
        pytest.fail("Invalid discovery input reached schema resolution")

    monkeypatch.setattr(goal_tools, "tool_schema", unexpected_schema)
    with pytest.raises(ValueError, match="inspect_tool"):
        asyncio.run(goal_tools.run_read_tool(SimpleNamespace(), "inspect_tool", arguments))
