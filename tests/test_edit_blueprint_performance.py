from __future__ import annotations

from decimal import Decimal

from katcha.services.edit_blueprint_performance import (
    _aggregate_group,
    _comparison_summary,
    _group_identity,
)


def _identity(
    *,
    key: str = "persona_commentary",
    revision: int = 1,
    contract: str = "1.0.0",
    source_kind: str = "production",
    scope: str = "short",
    style: str = "observational",
) -> dict[str, object]:
    return _group_identity(
        {
            "edit_blueprint_key": key,
            "edit_blueprint_revision": revision,
            "edit_blueprint_contract_version": contract,
            "edit_composition": "blueprint_video",
            "edit_narration_mode": "persona_voice",
            "edit_source_layout_mode": "full_frame",
            "selected_style": style,
            "source_kind": source_kind,
            "source_scope": scope,
            "brand_key": "channel-brand",
            "brand_version": 1,
        }
    )


def _row(
    index: int,
    *,
    revenue: Decimal | None = Decimal("2.00"),
    cost: Decimal = Decimal("0.25"),
    view_percentage: float = 72.0,
    outcome: float = 0.75,
) -> dict[str, object]:
    return {
        "publication_id": f"publication-{index}",
        "sampled_at": f"2026-09-{10 + index:02d}T12:00:00+00:00",
        "views": 1000 + index,
        "engaged_views": 800 + index,
        "average_view_duration": 31.0,
        "average_view_percentage": view_percentage,
        "likes": 100,
        "comments": 20,
        "shares": 10,
        "subscribers_gained": 3,
        "subscribers_lost": 0,
        "estimated_revenue": revenue,
        "outcome_score": outcome,
        "attributed_cost_usd": cost,
        "retention_25": 0.88,
        "retention_50": 0.73,
        "retention_75": 0.55,
        "retention_95": 0.37,
    }


def test_group_identity_isolates_blueprint_revision_and_treatment() -> None:
    first = _identity(revision=1, style="observational")
    second = _identity(revision=2, style="observational")
    third = _identity(revision=1, style="interactive")

    assert first["group_key"] != second["group_key"]
    assert first["group_key"] != third["group_key"]
    assert first["source_scope"] == second["source_scope"]


def test_missing_revenue_is_not_treated_as_zero_margin() -> None:
    identity = _identity()
    rows = [
        _row(1, revenue=Decimal("2.00"), cost=Decimal("0.25")),
        _row(2, revenue=None, cost=Decimal("0.50")),
    ]

    aggregate = _aggregate_group(identity, rows)

    assert aggregate["publication_count"] == 2
    assert aggregate["revenue_covered_publications"] == 1
    assert aggregate["monetary_coverage"] == 0.5
    assert aggregate["covered_revenue_usd"] == "2.00"
    assert aggregate["covered_cost_usd"] == "0.25"
    assert aggregate["covered_contribution_margin_usd"] == "1.75"
    assert aggregate["attributed_cost_usd"] == "0.75"


def test_comparison_requires_same_scope_and_minimum_samples() -> None:
    left = _aggregate_group(
        _identity(key="persona_commentary", revision=1),
        [_row(index, view_percentage=72.0, outcome=0.75) for index in range(1, 6)],
    )
    right = _aggregate_group(
        _identity(key="header_explainer", revision=1),
        [_row(index, view_percentage=68.0, outcome=0.70) for index in range(6, 11)],
    )
    other_scope = _aggregate_group(
        _identity(
            key="persona_commentary",
            revision=2,
            source_kind="short_episode",
            scope="ranksnaxx_countdown",
        ),
        [_row(index) for index in range(11, 16)],
    )

    status, summary = _comparison_summary([left, right, other_scope])

    assert status == "evidence_available"
    comparisons = summary["comparisons"]
    assert len(comparisons) == 1
    comparison = comparisons[0]
    assert comparison["source_scope"] == "short"
    assert comparison["observed_delta_left_minus_right"]["average_view_percentage"] == 4.0


def test_small_samples_remain_advisory_and_insufficient() -> None:
    left = _aggregate_group(
        _identity(key="persona_commentary"),
        [_row(1), _row(2)],
    )
    right = _aggregate_group(
        _identity(key="header_explainer"),
        [_row(3), _row(4)],
    )

    status, summary = _comparison_summary([left, right])

    assert status == "insufficient_data"
    assert summary["advisory_only"] is True
    assert summary["comparisons"] == []
