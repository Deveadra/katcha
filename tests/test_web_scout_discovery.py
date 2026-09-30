from katcha.acquisition.adapters import available_adapters
from katcha.acquisition.web_scout import (
    WebScoutDiscoveryAdapter,
    _next_scout_cursor,
    _response_output_text,
    _web_search_tool,
    parse_web_scout_output,
)
from katcha.integrations.chatgpt import ChatGPTWebSearchInference


def test_web_scout_adapter_is_registered() -> None:
    catalog = {(row["key"], row["version"]): row for row in available_adapters()}

    assert ("web_scout", "v1") in catalog
    assert "tiktok" in catalog[("web_scout", "v1")]["supported_platforms"]
    assert "bluesky" in catalog[("web_scout", "v1")]["supported_platforms"]


def test_web_scout_parser_only_accepts_grounded_urls() -> None:
    items = parse_web_scout_output(
        """
        {
          "items": [
            {
              "source_url": "https://www.tiktok.com/@newcreator/video/123",
              "title": "Fresh clip",
              "creator": "@newcreator",
              "why_relevant": "Fast-growing gaming clip"
            },
            {
              "source_url": "https://invented.example.invalid/post/9",
              "title": "Hallucinated"
            }
          ]
        }
        """,
        grounded_urls={"https://tiktok.com/@newcreator/video/123"},
        limit=10,
    )

    assert len(items) == 1
    assert items[0].source_url == "https://tiktok.com/@newcreator/video/123"
    assert items[0].metadata["platform"] == "tiktok"
    assert items[0].provenance_claims["grounded_search_result"] is True


def test_web_scout_targets_requested_social_domains() -> None:
    tool = _web_search_tool({"platforms": ["tiktok", "instagram", "bluesky"]})

    assert tool["type"] == "web_search"
    assert set(tool["filters"]["allowed_domains"]) == {
        "tiktok.com",
        "instagram.com",
        "bsky.app",
    }


def test_web_scout_keeps_bounded_exploration_memory() -> None:
    items = parse_web_scout_output(
        '{"items":[{"source_url":"https://bsky.app/profile/example/post/1"}]}',
        grounded_urls={"https://bsky.app/profile/example/post/1"},
        limit=10,
    )
    cursor = _next_scout_cursor(
        {
            "cycle": 4,
            "recent_sources": [
                "https://example.com/old-" + str(index)
                for index in range(65)
            ],
        },
        items,
    )

    assert cursor["cycle"] == 5
    assert len(cursor["recent_sources"]) <= 60
    assert cursor["recent_sources"][-1] == "https://bsky.app/profile/example/post/1"


def test_web_scout_extracts_responses_api_output_text() -> None:
    payload = {
        "output": [
            {"type": "web_search_call", "action": {"sources": []}},
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": '{"items":[]}',
                        "annotations": [],
                    }
                ],
            },
        ]
    }

    assert _response_output_text(payload) == '{"items":[]}'



def test_web_scout_uses_chatgpt_plan_without_api_key(monkeypatch) -> None:
    class Settings:
        ai_enabled = True
        chatgpt_host_id = "urn:uuid:test-host"
        openai_api_key = None

        @staticmethod
        def resolved_ai_execution_mode() -> str:
            return "live"

    payload = {
        "id": "resp_plan",
        "output": [
            {
                "type": "web_search_call",
                "action": {
                    "sources": [
                        {"url": "https://bsky.app/profile/new/post/123"}
                    ]
                },
            },
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": (
                            '{"items":[{"source_url":'
                            '"https://bsky.app/profile/new/post/123",'
                            '"title":"Fresh gaming post"}]}'
                        ),
                        "annotations": [],
                    }
                ],
            },
        ],
    }
    calls = []
    monkeypatch.setattr(
        "katcha.acquisition.web_scout.get_settings",
        lambda: Settings(),
    )
    monkeypatch.setattr(
        "katcha.acquisition.web_scout.invoke_web_search_json",
        lambda **kwargs: ChatGPTWebSearchInference(
            payload=payload,
            model="gpt-plan",
            input_tokens=120,
            output_tokens=45,
        ),
    )
    monkeypatch.setattr(
        "katcha.acquisition.web_scout.record_usage",
        lambda **kwargs: calls.append(kwargs),
    )

    batch = WebScoutDiscoveryAdapter().discover(
        {
            "operator_request": "Find new gaming sources",
            "platforms": ["bluesky"],
            "limit": 10,
        },
        {},
    )

    assert len(batch.items) == 1
    assert batch.items[0].source_url == "https://bsky.app/profile/new/post/123"
    assert batch.provider_usage == {"openai.web_search": 1}
    assert calls[0]["target"].provider == "chatgpt"
    assert calls[0]["cost_usd"] == 0
