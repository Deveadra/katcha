from katcha.editorial.generator import build_script_prompt
from katcha.editorial.personas import YOUTH_HOST_V1, YOUTH_HOST_V2, get_persona


def test_youth_host_versions_remain_retrievable() -> None:
    assert get_persona("youth_host", "1.0.0") is YOUTH_HOST_V1
    assert get_persona("youth_host", "2.0.0") is YOUTH_HOST_V2
    assert get_persona("youth_host") is YOUTH_HOST_V2


def test_youth_host_v2_prompt_contains_brand_guardrails() -> None:
    prompt = build_script_prompt(
        YOUTH_HOST_V2,
        {
            "duration_seconds": 12.5,
            "transcript": "example source transcript",
            "ai_features": {
                "bulk": {
                    "event_summary": "A person attempts a trick twice.",
                    "setup": "The first attempt looks confident.",
                    "payoff": "The second attempt fails unexpectedly.",
                }
            },
        },
        prompt_version="short-script-v2",
    )

    assert "Host persona: youth_host 2.0.0" in prompt
    assert "Hook rules:" in prompt
    assert "Trust rules:" in prompt
    assert "Allowed interaction rituals:" in prompt
    assert "official_ruling" in prompt
    assert "Never invent a factual detail" in prompt
    assert "same host, not three different personalities" in prompt
