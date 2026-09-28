import uuid

from katcha.models import ClipFeature
from katcha.services.clip_lifecycle import perceptual_similarity


def _features(*hashes: str) -> ClipFeature:
    return ClipFeature(
        clip_id=uuid.uuid4(),
        perceptual_hashes=list(hashes),
        keyframe_keys=[],
        local_features={},
        ai_features={},
        score_breakdown={},
    )


def test_perceptual_similarity_identical_samples_are_exact() -> None:
    left = _features(
        "0000000000000000",
        "1111111111111111",
        "2222222222222222",
    )
    right = _features(
        "0000000000000000",
        "1111111111111111",
        "2222222222222222",
    )
    assert perceptual_similarity(left, right) == 1.0


def test_perceptual_similarity_requires_enough_frames() -> None:
    left = _features("0000000000000000", "1111111111111111")
    right = _features("0000000000000000", "1111111111111111")
    assert perceptual_similarity(left, right) == 0.0


def test_perceptual_similarity_separates_different_content() -> None:
    left = _features(
        "0000000000000000",
        "0000000000000000",
        "0000000000000000",
    )
    right = _features(
        "ffffffffffffffff",
        "ffffffffffffffff",
        "ffffffffffffffff",
    )
    assert perceptual_similarity(left, right) == 0.0
