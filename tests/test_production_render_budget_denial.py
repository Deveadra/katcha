from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from temporalio.exceptions import ApplicationError

from katcha.orchestration import production_activities as activities
from katcha.rendering.client import RenderBudgetDenied, RendererRequestError


@pytest.mark.parametrize("denied", [True, False])
def test_render_activity_stops_budget_denial_and_preserves_transient_retry(monkeypatch, denied):
    production = SimpleNamespace(render_manifest={"version": "short-render-v1"})
    session = SimpleNamespace(get=lambda *args: production, scalar=lambda *args: None)

    @contextmanager
    def scope():
        yield session

    manifest = SimpleNamespace(output_key="production/test.mp4", version="short-render-v1")
    failure = (
        RenderBudgetDenied("monthly budget exceeded")
        if denied
        else RendererRequestError("HTTP 503")
    )
    renderer = Mock(side_effect=failure)
    record_failure = Mock()
    monkeypatch.setattr(activities, "session_scope", scope)
    monkeypatch.setattr(activities, "get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(
        activities, "ShortRenderManifest", SimpleNamespace(model_validate=lambda _: manifest)
    )
    monkeypatch.setattr(activities, "render_short", renderer)
    monkeypatch.setattr(
        activities, "ensure_render_attempt", lambda *args, **kwargs: SimpleNamespace(id=uuid4())
    )
    monkeypatch.setattr(activities, "mark_render_attempt_started", Mock())
    monkeypatch.setattr(activities, "mark_render_attempt_retryable_failure", record_failure)

    expected = ApplicationError if denied else RendererRequestError
    with pytest.raises(expected) as caught:
        activities.render_short_activity(str(uuid4()))
    renderer.assert_called_once()
    record_failure.assert_called_once()
    if denied:
        assert caught.value.non_retryable
        assert caught.value.type == "RenderBudgetDenied"
    else:
        assert caught.value is failure
