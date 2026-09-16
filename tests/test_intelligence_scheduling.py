from katcha.intelligence.scheduling import HistoricalSlot, recommend_windows


def test_sparse_history_uses_configured_fallback() -> None:
    history = [
        HistoricalSlot(weekday=0, hour_local=18, outcome_score=0.5),
        HistoricalSlot(weekday=2, hour_local=19, outcome_score=0.6),
    ]
    fallback = [
        {"weekday": 4, "hour_local": 18},
        {"weekday": 6, "hour_local": 17},
    ]

    result = recommend_windows(history, fallback_schedule=fallback, limit=2)

    assert [(item.weekday, item.hour_local) for item in result] == [(4, 18), (6, 17)]
    assert all(item.source == "configured_fallback" for item in result)
    assert all(item.confidence < 0.5 for item in result)


def test_sufficient_history_ranks_stronger_observed_window_first() -> None:
    history = [
        *[
            HistoricalSlot(weekday=5, hour_local=18, outcome_score=0.9)
            for _ in range(8)
        ],
        *[
            HistoricalSlot(weekday=2, hour_local=12, outcome_score=0.25)
            for _ in range(8)
        ],
    ]

    result = recommend_windows(history, fallback_schedule=[], limit=2)

    assert result[0].weekday == 5
    assert result[0].hour_local == 18
    assert result[0].source == "channel_history"
    assert result[0].score > result[1].score
    assert result[0].sample_count == 8
