from sqlalchemy import CheckConstraint, UniqueConstraint

from katcha.acquisition_models import (
    DiscoveryCandidate,
    DiscoveryObservation,
    DiscoveryRun,
    IngestionSource,
    RightsAssessment,
)
from katcha.api.acquisition import CandidateDetailResponse
from katcha.api.main import app


def _unique_columns(table) -> set[tuple[str, ...]]:
    return {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }


def test_discovery_run_candidate_and_observation_idempotency_contracts() -> None:
    assert ("adapter_key", "run_key") in _unique_columns(DiscoveryRun.__table__)
    candidate_unique = _unique_columns(DiscoveryCandidate.__table__)
    assert ("adapter_key", "external_id") in candidate_unique
    assert ("canonical_url",) in candidate_unique
    assert ("source_item_id",) in candidate_unique
    assert (
        "discovery_run_id",
        "discovery_candidate_id",
    ) in _unique_columns(DiscoveryObservation.__table__)
    assert ("source_key",) in _unique_columns(IngestionSource.__table__)


def test_candidate_detail_exposes_complete_observation_lineage() -> None:
    assert "observations" in CandidateDetailResponse.model_fields


def test_rights_assessments_are_immutable_versioned_rows() -> None:
    assert ("discovery_candidate_id", "version") in _unique_columns(
        RightsAssessment.__table__
    )
    checks = {
        constraint.name
        for constraint in RightsAssessment.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert "ck_rights_assessment_version_positive" in checks


def test_discovery_and_rights_routes_are_mounted() -> None:
    paths = set(app.openapi()["paths"])

    assert "/v1/discovery/runs" in paths
    assert "/v1/discovery/sources" in paths
    assert "/v1/discovery/sources/{source_id}/runs" in paths
    assert "/v1/discovery/sources/{source_id}/imports" in paths
    assert "/v1/discovery/runs/{run_id}/execute" in paths
    assert "/v1/discovery/candidates" in paths
    assert "/v1/discovery/candidates/{candidate_id}" in paths
    assert "/v1/discovery/candidates/{candidate_id}/assessments" in paths
    assert "/v1/rights/assessments/{assessment_id}/evidence" in paths
    assert "/v1/discovery/candidates/{candidate_id}/promote" in paths
