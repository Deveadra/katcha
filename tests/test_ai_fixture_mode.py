from __future__ import annotations

import io
import subprocess
import wave

import katcha.ai.fixtures
import katcha.ai.providers
import katcha.audio.tts
import katcha.config
import katcha.editorial.episode_generator
import katcha.editorial.personas


PLAN = {
    "ordered_items": [
        {"position": 5, "candidate_id": "clip-5", "role": "opener"},
        {"position": 4, "candidate_id": "clip-4", "role": "build"},
        {"position": 3, "candidate_id": "clip-3", "role": "build"},
        {"position": 2, "candidate_id": "clip-2", "role": "false_peak"},
        {"position": 1, "candidate_id": "clip-1", "role": "payoff"},
    ]
}


def _wav_bytes() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(24000)
        handle.writeframes(b"\x00\x00" * 2400)
    return buffer.getvalue()


def test_development_auto_mode_resolves_to_fixture() -> None:
    settings = katcha.config.Settings(env="development", ai_execution_mode="auto")
    assert settings.resolved_ai_execution_mode() == "fixture"


def test_production_auto_mode_resolves_to_live() -> None:
    settings = katcha.config.Settings(env="production", ai_execution_mode="auto")
    assert settings.resolved_ai_execution_mode() == "live"


def test_fixture_vision_never_requires_provider_keys() -> None:
    settings = katcha.config.Settings(
        env="development",
        ai_enabled=True,
        ai_execution_mode="fixture",
        openai_api_key=None,
        gemini_api_key=None,
    )
    result = katcha.ai.providers.analyze_contact_sheet(
        b"not-used-in-fixture-mode",
        None,
        reference_id="fixture-1",
        settings=settings,
    )
    assert result.target.provider == "fixture"
    assert result.input_tokens == 0
    assert result.output_tokens == 0
    assert result.value == katcha.ai.fixtures.fixture_clip_vision("fixture-1")


def test_fixture_ranked_script_preserves_plan_without_paid_provider() -> None:
    settings = katcha.config.Settings(
        env="development",
        ai_enabled=True,
        ai_execution_mode="fixture",
        openai_api_key=None,
        gemini_api_key=None,
    )
    result = katcha.editorial.episode_generator.generate_ranked_episode_scripts(
        persona=katcha.editorial.personas.get_persona("youth_host"),
        premise="fixture",
        plan_snapshot=PLAN,
        items=[],
        prompt_version="fixture",
        episode_id="episode-1",
        channel_profile_id=__import__("uuid").uuid4(),
        expected_value=0.8,
        settings=settings,
    )
    assert result.target.provider == "fixture"
    assert result.input_tokens == 0
    assert result.output_tokens == 0
    katcha.editorial.episode_generator.validate_episode_scripts_against_plan(result.scripts, PLAN)


def test_fixture_script_helpers_return_three_treatments() -> None:
    assert len(katcha.ai.fixtures.fixture_short_scripts().candidates) == 3
    assert len(katcha.ai.fixtures.fixture_ranked_episode_scripts(PLAN).candidates) == 3


def test_fixture_tts_uses_local_engine_and_zero_cost(monkeypatch) -> None:
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=_wav_bytes(),
            stderr=b"",
        )

    monkeypatch.setattr(katcha.audio.tts.subprocess, "run", fake_run)
    settings = katcha.config.Settings(
        env="development",
        ai_enabled=True,
        ai_execution_mode="fixture",
        openai_api_key=None,
        gemini_api_key=None,
    )
    result = katcha.audio.tts.synthesize_speech("fixture voiceover", settings=settings)

    assert result.target.provider == "fixture"
    assert result.profile.key == "fixture_youth_v1"
    assert result.estimated_cost_usd == 0
    assert result.cost_metadata["external_api_cost_usd"] == "0"


def test_fixture_packaging_is_grounded_and_zero_provider() -> None:
    candidates = katcha.ai.fixtures.fixture_packaging_candidates(
        {
            "current_title": "Existing title",
            "grounding_facts": ["Synthetic fixture fact."],
        },
        3,
    )
    assert len(candidates.candidates) == 3
    assert all(
        candidate.supporting_facts == ["Synthetic fixture fact."]
        for candidate in candidates.candidates
    )


def test_fixture_longform_critic_passes_without_provider() -> None:
    critique = katcha.ai.fixtures.fixture_longform_critique()
    assert critique.verdict == "pass"
    assert critique.issues == []
