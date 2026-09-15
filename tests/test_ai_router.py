from katcha.ai.router import route_for
from katcha.domain import AITask


def test_deep_video_prefers_gemini() -> None:
    route = route_for(AITask.DEEP_VIDEO)
    assert route.primary.provider == "gemini"


def test_short_script_prefers_terra_tier() -> None:
    route = route_for(AITask.SHORT_SCRIPT)
    assert route.primary.provider == "openai"
    assert "terra" in route.primary.model
