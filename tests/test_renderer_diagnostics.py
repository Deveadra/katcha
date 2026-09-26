import httpx
import pytest

from katcha.rendering.client import RendererRequestError, _raise_for_renderer_error
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
