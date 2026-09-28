from datetime import date

from katcha.config import Settings
from katcha.integrations.youtube import analytics
from katcha.integrations.youtube.analytics import channel_growth_metrics, report_rows


def test_analytics_rows_are_mapped_by_header_name() -> None:
    payload = {
        "columnHeaders": [{"name": "views"}, {"name": "likes"}],
        "rows": [[123, 4]],
    }

    assert report_rows(payload) == [{"views": 123, "likes": 4}]


def test_analytics_offsets_are_sorted_and_deduplicated() -> None:
    settings = Settings(_env_file=None, youtube_analytics_offsets_hours="24,1,6,1,72")

    assert settings.analytics_offsets_hours() == [1, 6, 24, 72]



def test_channel_growth_metrics_separate_longform_and_shorts(monkeypatch) -> None:
    def fake_query(*_args, start_date, metrics, **_kwargs):
        if metrics == ("estimatedMinutesWatched",):
            assert start_date == date(2025, 9, 29)
            return {
                "columnHeaders": [
                    {"name": "creatorContentType"},
                    {"name": "estimatedMinutesWatched"},
                ],
                "rows": [
                    ["VIDEO_ON_DEMAND", 6000],
                    ["LIVE_STREAM", 600],
                    ["SHORTS", 9000],
                ],
            }
        assert metrics == ("engagedViews",)
        assert start_date == date(2026, 7, 1)
        return {
            "columnHeaders": [
                {"name": "creatorContentType"},
                {"name": "engagedViews"},
            ],
            "rows": [["SHORTS", 321000], ["VIDEO_ON_DEMAND", 100]],
        }

    monkeypatch.setattr(analytics, "_query", fake_query)

    result = channel_growth_metrics(
        "00000000-0000-0000-0000-000000000001",
        as_of=date(2026, 9, 28),
    )

    assert result["estimated_qualified_watch_hours_365d"] == 110
    assert result["estimated_qualified_shorts_views_90d"] == 321000
