from katcha.ai.gemini_capacity import (
    capacity_targets,
    explicit_model_capacity_rejection,
    run_with_gemini_capacity_fallback,
)
from katcha.ai.router import ModelTarget


class _ProviderError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


def test_gemini_38_has_stable_capacity_fallbacks() -> None:
    targets = capacity_targets(ModelTarget("gemini", "gemini-3.8-flash"))
    assert [target.model for target in targets] == [
        "gemini-3.8-flash",
        "gemini-3.7-flash",
        "gemini-3.6-flash",
    ]


def test_high_demand_503_is_explicit_capacity_rejection() -> None:
    error = _ProviderError(
        503,
        "503 UNAVAILABLE: This model is currently experiencing high demand.",
    )
    assert explicit_model_capacity_rejection(error) is True


def test_ambiguous_503_does_not_trigger_model_switch() -> None:
    error = _ProviderError(503, "upstream request failed")
    assert explicit_model_capacity_rejection(error) is False


def test_capacity_fallback_moves_to_next_model_only_after_explicit_rejection() -> None:
    calls: list[str] = []

    def invoke(target: ModelTarget) -> str:
        calls.append(target.model)
        if target.model == "gemini-3.8-flash":
            raise _ProviderError(503, "Service unavailable due to high demand")
        return target.model

    result = run_with_gemini_capacity_fallback(
        ModelTarget("gemini", "gemini-3.8-flash"),
        invoke,
    )

    assert result == "gemini-3.7-flash"
    assert calls == ["gemini-3.8-flash", "gemini-3.7-flash"]
