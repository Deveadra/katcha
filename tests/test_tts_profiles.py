from katcha.ai.router import ModelTarget
from katcha.audio.tts import choose_voice_profile, get_voice_profile, voice_profile_for_target
from katcha.config import Settings


def test_voice_profile_prefers_requested_legacy_openai_when_configured() -> None:
    settings = Settings(
        openai_api_key="test-openai",
        gemini_api_key="test-gemini",
        tts_profile="openai_youth_v1",
    )
    profile = choose_voice_profile(settings)
    assert profile.key == "openai_youth_v1"
    assert profile.provider == "openai"


def test_voice_profile_falls_back_to_current_brand_when_requested_provider_missing() -> None:
    settings = Settings(
        openai_api_key=None,
        gemini_api_key="test-gemini",
        tts_profile="openai_youth_v1",
    )
    profile = choose_voice_profile(settings)
    assert profile.key == "gemini_youth_v2"
    assert profile.provider == "gemini"


def test_default_settings_select_branded_openai_voice_v2() -> None:
    settings = Settings(
        openai_api_key="test-openai",
        gemini_api_key="test-gemini",
    )
    profile = choose_voice_profile(settings)
    assert profile.key == "openai_youth_v2"
    assert profile.version == "2"


def test_provider_target_prefers_latest_brand_direction() -> None:
    target = ModelTarget("openai", "gpt-4o-mini-tts-2025-12-15")
    assert voice_profile_for_target(target).key == "openai_youth_v2"


def test_legacy_voice_profiles_remain_addressable() -> None:
    assert get_voice_profile("openai_youth_v1").version == "1"
    assert get_voice_profile("gemini_youth_v1").version == "1"
