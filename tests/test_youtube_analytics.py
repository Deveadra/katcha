from katcha.config import Settings
from katcha.integrations.youtube.analytics import report_rows


def test_analytics_rows_are_mapped_by_header_name() -> None:
    payload = {
        "columnHeaders": [{"name": "views"}, {"name": "likes"}],
        "rows": [[123, 4]],
    }

    assert report_rows(payload) == [{"views": 123, "likes": 4}]


def test_analytics_offsets_are_sorted_and_deduplicated() -> None:
    settings = Settings(_env_file=None, youtube_analytics_offsets_hours="24,1,6,1,72")

    assert settings.analytics_offsets_hours() == [1, 6, 24, 72]
