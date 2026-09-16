from __future__ import annotations

from dataclasses import dataclass

from katcha.domain import (
    AudioRightsStatus,
    GateStatus,
    RightsBasis,
    RightsLane,
)

_GREEN_BASES = {
    RightsBasis.OWNED,
    RightsBasis.DIRECT_PERMISSION,
    RightsBasis.LICENSED,
    RightsBasis.CC0,
    RightsBasis.CC_BY,
    RightsBasis.PUBLIC_DOMAIN,
}
_EVIDENCE_REQUIRED_BASES = {
    RightsBasis.DIRECT_PERMISSION,
    RightsBasis.LICENSED,
    RightsBasis.CC0,
    RightsBasis.CC_BY,
    RightsBasis.PUBLIC_DOMAIN,
}
_BLOCKING_RISK_FLAGS = {
    "child_sexual_exploitation",
    "extreme_graphic",
    "nonconsensual_intimate",
}
_REVIEW_RISK_FLAGS = {
    "identifiable_minor",
    "minor_embarrassing",
    "privacy_sensitive",
    "publicity_sensitive",
    "graphic",
    "sexual",
    "hate",
    "dangerous_act",
    "medical_sensitive",
}
_CLEARED_AUDIO = {
    AudioRightsStatus.ORIGINAL,
    AudioRightsStatus.CLEARED,
}


@dataclass(frozen=True, slots=True)
class AcquisitionPolicyResult:
    rights_lane: RightsLane
    rights_gate: GateStatus
    production_eligible: bool
    reasons: tuple[str, ...]
    advisories: tuple[str, ...]


def evaluate_acquisition_policy(
    *,
    rights_basis: RightsBasis,
    audio_status: AudioRightsStatus,
    originality_gate: GateStatus,
    risk_flags: list[str] | tuple[str, ...] = (),
    operator_authorized: bool = False,
    evidence_present: bool = False,
) -> AcquisitionPolicyResult:
    normalized_flags = {
        str(value).strip().casefold()
        for value in risk_flags
        if str(value).strip()
    }
    reasons: list[str] = []
    advisories: list[str] = []

    if rights_basis == RightsBasis.BLOCKED:
        rights_gate = GateStatus.BLOCKED
        reasons.append("rights_basis_blocked")
    elif rights_basis in _GREEN_BASES:
        rights_gate = GateStatus.CLEARED
        if rights_basis in _EVIDENCE_REQUIRED_BASES and not evidence_present:
            rights_gate = GateStatus.REVIEW_REQUIRED
            reasons.append("rights_evidence_required")
    elif rights_basis == RightsBasis.FAIR_USE_CANDIDATE:
        if operator_authorized:
            rights_gate = GateStatus.CLEARED
            advisories.append("fair_use_authorization_is_not_a_legal_determination")
        else:
            rights_gate = GateStatus.REVIEW_REQUIRED
            reasons.append("fair_use_candidate_requires_operator_review")
    else:
        rights_gate = GateStatus.REVIEW_REQUIRED
        reasons.append("rights_basis_unknown")

    blocking_flags = sorted(normalized_flags & _BLOCKING_RISK_FLAGS)
    review_flags = sorted(normalized_flags & _REVIEW_RISK_FLAGS)
    if blocking_flags:
        rights_gate = GateStatus.BLOCKED
        reasons.extend(f"risk_blocked:{flag}" for flag in blocking_flags)
    elif review_flags and not operator_authorized:
        if rights_gate != GateStatus.BLOCKED:
            rights_gate = GateStatus.REVIEW_REQUIRED
        reasons.extend(f"risk_review:{flag}" for flag in review_flags)
    elif review_flags:
        advisories.extend(f"risk_operator_reviewed:{flag}" for flag in review_flags)

    if audio_status == AudioRightsStatus.BLOCKED:
        reasons.append("audio_blocked")
    elif audio_status == AudioRightsStatus.REPLACE_REQUIRED:
        reasons.append("audio_replacement_required")
    elif audio_status == AudioRightsStatus.REVIEW_REQUIRED:
        reasons.append("audio_review_required")

    if originality_gate == GateStatus.BLOCKED:
        reasons.append("originality_blocked")
    elif originality_gate == GateStatus.REVIEW_REQUIRED:
        reasons.append("originality_review_required")

    any_blocked = (
        rights_gate == GateStatus.BLOCKED
        or audio_status == AudioRightsStatus.BLOCKED
        or originality_gate == GateStatus.BLOCKED
    )
    any_review = (
        rights_gate != GateStatus.CLEARED
        or audio_status not in _CLEARED_AUDIO
        or originality_gate != GateStatus.CLEARED
    )

    if any_blocked:
        lane = RightsLane.RED
    elif (
        rights_basis == RightsBasis.FAIR_USE_CANDIDATE
        or rights_basis == RightsBasis.UNKNOWN
        or any_review
        or bool(review_flags)
    ):
        lane = RightsLane.YELLOW
    else:
        lane = RightsLane.GREEN

    production_eligible = (
        rights_gate == GateStatus.CLEARED
        and audio_status in _CLEARED_AUDIO
        and originality_gate == GateStatus.CLEARED
        and not any_blocked
    )
    return AcquisitionPolicyResult(
        rights_lane=lane,
        rights_gate=rights_gate,
        production_eligible=production_eligible,
        reasons=tuple(dict.fromkeys(reasons)),
        advisories=tuple(dict.fromkeys(advisories)),
    )
