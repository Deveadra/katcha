from katcha.longform.scoring import CandidateSignals, score_pool, sequence_pool


def _candidate(
    clip_id: str,
    *,
    feature: float,
    hook: float,
    payoff: float,
    category: str,
    views: int,
) -> CandidateSignals:
    return CandidateSignals(
        clip_id=clip_id,
        duration_seconds=8,
        feature_score=feature,
        hook_score=hook,
        payoff_score=payoff,
        surprise_score=feature,
        rewatch_score=feature,
        views=views,
        engaged_views=views,
        average_view_percentage=90,
        shares=max(1, views // 100),
        comments=max(1, views // 200),
        subscribers_gained=max(0, views // 1000),
        categories=(category,),
        tone=("funny",),
    )


def test_score_pool_prefers_strong_measured_candidate() -> None:
    weak = _candidate("weak", feature=45, hook=50, payoff=40, category="animal", views=100)
    strong = _candidate(
        "strong", feature=92, hook=94, payoff=90, category="fail", views=100_000
    )

    scored = score_pool([weak, strong])

    assert scored[0].signals.clip_id == "strong"
    assert scored[0].deterministic_score > scored[1].deterministic_score
    assert scored[0].opening_score > scored[1].opening_score


def test_sequence_pool_uses_opening_score_and_unique_clips() -> None:
    candidates = [
        _candidate(
            f"clip-{index}",
            feature=60 + index,
            hook=99 if index == 2 else 55 + index,
            payoff=70 + index,
            category="animal" if index % 2 == 0 else "fail",
            views=1_000 * (index + 1),
        )
        for index in range(8)
    ]
    scored = score_pool(candidates)

    sequence = sequence_pool(
        scored,
        target_duration_seconds=32,
        min_segments=4,
        max_segments=6,
        target_segment_count=5,
    )

    assert sequence[0].signals.clip_id == "clip-2"
    assert 4 <= len(sequence) <= 6
    assert len({item.signals.clip_id for item in sequence}) == len(sequence)


def test_sequence_pool_rejects_insufficient_candidates() -> None:
    scored = score_pool(
        [_candidate("only", feature=80, hook=80, payoff=80, category="fail", views=1000)]
    )

    try:
        sequence_pool(
            scored,
            target_duration_seconds=30,
            min_segments=3,
            max_segments=5,
        )
    except ValueError as exc:
        assert "not enough eligible candidates" in str(exc)
    else:
        raise AssertionError("expected insufficient-candidate failure")
