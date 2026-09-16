from katcha.intelligence.learning import TrainingRow, blended_score, train_ranking


def _row(index: int, count: int) -> TrainingRow:
    hook = index / max(1, count - 1)
    return TrainingRow(
        features={
            "baseline_score": 0.45,
            "hook_score": hook,
            "surprise_score": hook * 0.8,
            "humor_score": hook * 0.6,
            "comment_potential": hook * 0.7,
            "rewatch_potential": hook * 0.9,
            "duration_signal": 0.4,
        },
        outcome=hook,
    )


def test_sparse_history_keeps_learned_weight_zero() -> None:
    result = train_ranking([_row(index, 8) for index in range(8)])

    assert result.algorithm == "baseline-only-v1"
    assert result.blend_ratio == 0.0
    assert result.confidence == 0.0
    assert result.validation_metrics["reason"] == "insufficient_samples"


def test_validated_learning_can_earn_bounded_influence() -> None:
    rows = [_row(index, 30) for index in range(30)]
    result = train_ranking(rows)

    assert result.algorithm == "ridge-v1"
    assert 0.0 < result.blend_ratio <= 0.65
    assert 0.0 < result.confidence <= 1.0
    assert result.validation_metrics["learned_mae"] < result.validation_metrics["baseline_mae"]


def test_blended_score_never_escapes_probability_range() -> None:
    result = train_ranking([_row(index, 30) for index in range(30)])

    low, low_details = blended_score({"baseline_score": -4.0}, result)
    high, high_details = blended_score(
        {"baseline_score": 8.0, "hook_score": 8.0},
        result,
    )

    assert 0.0 <= low <= 1.0
    assert 0.0 <= high <= 1.0
    assert low_details["algorithm"] == result.algorithm
    assert high_details["algorithm"] == result.algorithm
