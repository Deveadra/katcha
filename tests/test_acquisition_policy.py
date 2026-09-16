from katcha.acquisition.policy import evaluate_acquisition_policy
from katcha.domain import (
    AudioRightsStatus,
    GateStatus,
    RightsBasis,
    RightsLane,
)


def test_license_basis_requires_stored_evidence() -> None:
    result = evaluate_acquisition_policy(
        rights_basis=RightsBasis.LICENSED,
        audio_status=AudioRightsStatus.CLEARED,
        originality_gate=GateStatus.CLEARED,
        evidence_present=False,
    )

    assert result.rights_lane == RightsLane.YELLOW
    assert result.rights_gate == GateStatus.REVIEW_REQUIRED
    assert result.production_eligible is False
    assert "rights_evidence_required" in result.reasons


def test_evidence_backed_license_can_clear_all_gates() -> None:
    result = evaluate_acquisition_policy(
        rights_basis=RightsBasis.LICENSED,
        audio_status=AudioRightsStatus.CLEARED,
        originality_gate=GateStatus.CLEARED,
        evidence_present=True,
    )

    assert result.rights_lane == RightsLane.GREEN
    assert result.rights_gate == GateStatus.CLEARED
    assert result.production_eligible is True


def test_fair_use_candidate_never_becomes_green_lane() -> None:
    pending = evaluate_acquisition_policy(
        rights_basis=RightsBasis.FAIR_USE_CANDIDATE,
        audio_status=AudioRightsStatus.ORIGINAL,
        originality_gate=GateStatus.CLEARED,
    )
    reviewed = evaluate_acquisition_policy(
        rights_basis=RightsBasis.FAIR_USE_CANDIDATE,
        audio_status=AudioRightsStatus.ORIGINAL,
        originality_gate=GateStatus.CLEARED,
        operator_authorized=True,
    )

    assert pending.rights_lane == RightsLane.YELLOW
    assert pending.production_eligible is False
    assert reviewed.rights_lane == RightsLane.YELLOW
    assert reviewed.production_eligible is True
    assert "fair_use_authorization_is_not_a_legal_determination" in reviewed.advisories


def test_audio_and_originality_are_independent_fail_closed_gates() -> None:
    audio = evaluate_acquisition_policy(
        rights_basis=RightsBasis.OWNED,
        audio_status=AudioRightsStatus.REPLACE_REQUIRED,
        originality_gate=GateStatus.CLEARED,
    )
    originality = evaluate_acquisition_policy(
        rights_basis=RightsBasis.OWNED,
        audio_status=AudioRightsStatus.ORIGINAL,
        originality_gate=GateStatus.BLOCKED,
    )

    assert audio.production_eligible is False
    assert audio.rights_lane == RightsLane.YELLOW
    assert "audio_replacement_required" in audio.reasons
    assert originality.production_eligible is False
    assert originality.rights_lane == RightsLane.RED
    assert "originality_blocked" in originality.reasons


def test_reviewed_minor_risk_remains_visible_as_yellow() -> None:
    pending = evaluate_acquisition_policy(
        rights_basis=RightsBasis.OWNED,
        audio_status=AudioRightsStatus.ORIGINAL,
        originality_gate=GateStatus.CLEARED,
        risk_flags=["minor_embarrassing"],
    )
    reviewed = evaluate_acquisition_policy(
        rights_basis=RightsBasis.OWNED,
        audio_status=AudioRightsStatus.ORIGINAL,
        originality_gate=GateStatus.CLEARED,
        risk_flags=["minor_embarrassing"],
        operator_authorized=True,
    )

    assert pending.production_eligible is False
    assert pending.rights_lane == RightsLane.YELLOW
    assert reviewed.production_eligible is True
    assert reviewed.rights_lane == RightsLane.YELLOW


def test_hard_blocking_risk_cannot_be_operator_overridden() -> None:
    result = evaluate_acquisition_policy(
        rights_basis=RightsBasis.OWNED,
        audio_status=AudioRightsStatus.ORIGINAL,
        originality_gate=GateStatus.CLEARED,
        risk_flags=["nonconsensual_intimate"],
        operator_authorized=True,
    )

    assert result.rights_gate == GateStatus.BLOCKED
    assert result.rights_lane == RightsLane.RED
    assert result.production_eligible is False
