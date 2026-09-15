from katcha.audio.tts import choose_voice_profile
from katcha.config import Settings


def test_voice_profile_prefers_requested_openai_when_configured() -> None:
    settings = Settings(
        openai_api_key="test-openai",
        gemini_api_key="test-gemini",
        tts_profile="openai_youth_v1",
    )
    profile = choose_voice_profile(settings)
    assert profile.key == "openai_youth_v1"
    assert profile.provider == "openai"


def test_voice_profile_falls_back_when_requested_provider_missing() -> None:
    settings = Settings(
        openai_api_key=None,
        gemini_api_key="test-gemini",
        tts_profile="openai_youth_v1",
    )
    profile = choose_voice_profile(settings)
    assert profile.key == "gemini_youth_v1"
    assert profile.provider == "gemini"
