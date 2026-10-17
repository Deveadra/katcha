import httpx
import pytest

from katcha.rendering.client import (
    RenderBudgetDenied,
    RendererRequestError,
    _raise_for_renderer_error,
)
from katcha.services.render_recovery import _dead_letter_error


def test_renderer_http_error_includes_service_error_body() -> None:
    response = httpx.Response(
        500,
        request=httpx.Request("POST", "http://renderer:8787/render"),
        json={"error": "remotion failed to decode narration audio"},
    )

    with pytest.raises(
        RendererRequestError,
        match="renderer HTTP 500: remotion failed to decode narration audio",
    ):
        _raise_for_renderer_error(response)


def test_dead_letter_keeps_detailed_attempt_error_over_temporal_wrapper() -> None:
    detailed = "renderer HTTP 500: remotion failed to decode narration audio"

    assert _dead_letter_error(detailed, "Activity task failed") == detailed


def test_dead_letter_appends_non_generic_workflow_error() -> None:
    detailed = "renderer HTTP 500: source fetch failed"

    result = _dead_letter_error(detailed, "workflow timed out")

    assert detailed in result
    assert "workflow timed out" in result


def test_budget_denial_is_distinct_from_uncertain_renderer_failure() -> None:
    response = httpx.Response(
        402,
        json={"code": "render_budget_denied", "error": "monthly budget exceeded"},
    )
    with pytest.raises(RenderBudgetDenied, match="monthly budget exceeded"):
        _raise_for_renderer_error(response)


@pytest.mark.parametrize("status,code", [(500, "render_budget_denied"), (402, "other")])
def test_unclassified_renderer_error_remains_retryable(status, code) -> None:
    with pytest.raises(RendererRequestError) as error:
        _raise_for_renderer_error(httpx.Response(status, json={"code": code, "error": "failed"}))
    assert not isinstance(error.value, RenderBudgetDenied)
