from katcha.services.scoring import score_candidate


def test_score_candidate_rewards_real_source_signal() -> None:
    weak = score_candidate(
        local_features={
            "duration_seconds": 18,
            "mean_hash_distance": 4,
            "transcript_word_count": 5,
        },
        source_metrics={"view_count": 1_000, "like_count": 10, "comment_count": 1},
    )
    strong = score_candidate(
        local_features={
            "duration_seconds": 18,
            "mean_hash_distance": 12,
            "transcript_word_count": 20,
        },
        source_metrics={
            "view_count": 2_000_000,
            "like_count": 180_000,
            "comment_count": 12_000,
        },
    )
    assert strong.score > weak.score
    assert 0 <= weak.score <= 100
    assert 0 <= strong.score <= 100


def test_score_candidate_is_deterministic() -> None:
    kwargs = {
        "local_features": {
            "duration_seconds": 22,
            "mean_hash_distance": 9.5,
            "transcript_word_count": 11,
        },
        "source_metrics": {"view_count": 42_000, "like_count": 4_200, "comment_count": 210},
    }
    assert score_candidate(**kwargs) == score_candidate(**kwargs)
