from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from katcha.publishing_models import Publication, PublicationAnalyticsSnapshot
from katcha.services.edit_blueprint_performance import (
    _aggregate_group,
    _comparison_summary,
    _group_identity,
    _production_lineage_cost,
    _select_maturity_snapshot,
    _source_lineage,
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
        "subscribers_net": 3,
        "interaction_rate": 0.13,
        "estimated_revenue": revenue,
        "outcome_score": outcome,
        "attributed_cost_usd": cost,
        "retention_25": 0.88,
        "retention_50": 0.73,
        "retention_75": 0.55,
        "retention_95": 0.37,
        "has_retention": True,
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


def test_repeated_analytics_samples_choose_one_maturity_matched_snapshot() -> None:
    publication_id = uuid.uuid4()
    anchor = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    publication = Publication(
        id=publication_id,
        youtube_connection_id=uuid.uuid4(),
        workflow_id="publish-test",
        analytics_workflow_id="analytics-test",
        title="Test",
        published_at=anchor,
    )
    snapshots = [
        PublicationAnalyticsSnapshot(
            id=uuid.uuid4(),
            publication_id=publication_id,
            sample_key="6h",
            sampled_at=anchor + timedelta(hours=6),
            period_start=anchor.date(),
            period_end=anchor.date(),
        ),
        PublicationAnalyticsSnapshot(
            id=uuid.uuid4(),
            publication_id=publication_id,
            sample_key="70h",
            sampled_at=anchor + timedelta(hours=70),
            period_start=anchor.date(),
            period_end=anchor.date(),
        ),
        PublicationAnalyticsSnapshot(
            id=uuid.uuid4(),
            publication_id=publication_id,
            sample_key="80h",
            sampled_at=anchor + timedelta(hours=80),
            period_start=anchor.date(),
            period_end=anchor.date(),
        ),
    ]

    selected = _select_maturity_snapshot(publication, snapshots, 72)

    assert selected is not None
    assert selected.sample_key == "70h"


def test_margin_delta_is_suppressed_when_monetary_coverage_is_low() -> None:
    left_rows = [
        _row(index, revenue=Decimal("2.00") if index <= 3 else None)
        for index in range(1, 6)
    ]
    right_rows = [
        _row(index, revenue=Decimal("2.00") if index <= 8 else None)
        for index in range(6, 11)
    ]
    left = _aggregate_group(_identity(key="persona_commentary"), left_rows)
    right = _aggregate_group(_identity(key="header_explainer"), right_rows)

    _, summary = _comparison_summary([left, right])

    comparison = summary["comparisons"][0]
    assert left["monetary_coverage"] == 0.6
    assert right["monetary_coverage"] == 0.6
    assert "covered_margin_per_publication_usd" in (
        comparison["observed_delta_left_minus_right"]
    )

    right_low = _aggregate_group(
        _identity(key="header_explainer", revision=2),
        [
            _row(index, revenue=Decimal("2.00") if index < 13 else None)
            for index in range(11, 16)
        ],
    )
    assert right_low["monetary_coverage"] == 0.4
    _, low_summary = _comparison_summary([left, right_low])
    assert "covered_margin_per_publication_usd" not in (
        low_summary["comparisons"][0]["observed_delta_left_minus_right"]
    )


def test_retention_delta_requires_coverage_on_both_groups() -> None:
    left = _aggregate_group(
        _identity(key="persona_commentary"),
        [_row(index) for index in range(1, 6)],
    )
    sparse_rows = [_row(index) for index in range(6, 11)]
    for row in sparse_rows[2:]:
        row["has_retention"] = False
        for target in (25, 50, 75, 95):
            row[f"retention_{target}"] = None
    right = _aggregate_group(
        _identity(key="header_explainer"),
        sparse_rows,
    )

    _, summary = _comparison_summary([left, right])

    assert left["retention_coverage"] == 1.0
    assert right["retention_coverage"] == 0.4
    delta = summary["comparisons"][0]["observed_delta_left_minus_right"]
    assert "audience_watch_ratio_50pct" not in delta
    assert "audience_watch_ratio_95pct" not in delta


def test_publication_frozen_blueprint_lineage_overrides_live_source_state() -> None:
    source_id = uuid.uuid4()
    source = SimpleNamespace(
        id=source_id,
        kind="short",
        edit_blueprint_key="new_live_blueprint",
        edit_blueprint_version=9,
        edit_blueprint_snapshot={
            "version": "9.0.0",
            "composition": "new_composition",
            "narration": {"mode": "new_voice"},
            "source_layout": {"mode": "new_layout"},
        },
        estimated_cost_usd=Decimal("0.50"),
        parent_production_id=None,
        brand_key="brand",
        brand_version=4,
    )
    session = SimpleNamespace(get=lambda model, item_id: source)
    publication = Publication(
        id=uuid.uuid4(),
        production_id=source_id,
        youtube_connection_id=uuid.uuid4(),
        workflow_id="publish-frozen-lineage",
        analytics_workflow_id="analytics-frozen-lineage",
        title="Frozen lineage",
        treatment_metadata={
            "edit_blueprint_key": "persona_commentary",
            "edit_blueprint_revision": 2,
            "edit_blueprint_contract_version": "1.0.0",
            "edit_composition": "blueprint_video",
            "edit_narration_mode": "persona_voice",
            "edit_source_layout_mode": "full_frame",
            "source_scope": "short",
            "selected_style": "observational",
        },
    )

    result = _source_lineage(session, publication)

    assert result is not None
    _, _, lineage, _ = result
    assert lineage["edit_blueprint_key"] == "persona_commentary"
    assert lineage["edit_blueprint_revision"] == 2
    assert lineage["edit_blueprint_contract_version"] == "1.0.0"
    assert lineage["lineage_source"] == "publication_metadata"


def test_regeneration_ancestry_cost_includes_failed_parent_spend() -> None:
    parent_id = uuid.uuid4()
    child_id = uuid.uuid4()
    parent = SimpleNamespace(
        id=parent_id,
        estimated_cost_usd=Decimal("1.25"),
        parent_production_id=None,
    )
    child = SimpleNamespace(
        id=child_id,
        estimated_cost_usd=Decimal("0.75"),
        parent_production_id=parent_id,
    )
    sources = {parent_id: parent, child_id: child}
    session = SimpleNamespace(get=lambda model, item_id: sources.get(item_id))

    assert _production_lineage_cost(session, child) == Decimal("2.00")
