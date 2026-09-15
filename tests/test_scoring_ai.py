from katcha.services.scoring import score_candidate


def test_ai_features_change_candidate_score() -> None:
    local = {
        "duration_seconds": 18,
        "mean_hash_distance": 8,
        "transcript_word_count": 8,
    }
    source = {"view_count": 10_000, "like_count": 500, "comment_count": 20}
    baseline = score_candidate(local_features=local, source_metrics=source)
    enriched = score_candidate(
        local_features=local,
        source_metrics=source,
        ai_features={
            "bulk": {
                "hook_score": 95,
                "surprise_score": 90,
                "humor_score": 92,
                "comment_potential": 88,
                "rewatch_potential": 94,
            }
        },
    )
    assert enriched.score > baseline.score
    assert enriched.breakdown["ai_hook"] == 95
