import pytest

from katcha.domain import ClipStatus, require_clip_transition


def test_clip_happy_path_transition_is_allowed() -> None:
    require_clip_transition(ClipStatus.INGESTED, ClipStatus.NORMALIZED)


def test_clip_cannot_skip_pipeline_stage() -> None:
    with pytest.raises(ValueError, match="invalid clip transition"):
        require_clip_transition(ClipStatus.INGESTED, ClipStatus.PUBLISHED)


def test_failed_clip_is_terminal() -> None:
    with pytest.raises(ValueError):
        require_clip_transition(ClipStatus.FAILED, ClipStatus.INGESTED)
