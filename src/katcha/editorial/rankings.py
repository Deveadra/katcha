from __future__ import annotations

from typing import Literal, Sequence

from pydantic import BaseModel, Field


class RankingFormatContract(BaseModel):
    key: str
    version: str
    family: Literal["countdown"] = "countdown"
    default_item_count: int = Field(ge=1)
    allowed_item_counts: tuple[int, ...]
    premise_required: bool = True
    commentary_required: bool = True
    transformative_use_required: bool = True
    ordering_direction: Literal["descending"] = "descending"
    ordering_principles: tuple[str, ...]
    selection_dimensions: tuple[str, ...]


class RankingCandidateSignals(BaseModel):
    candidate_id: str = Field(min_length=1)
    hook_strength: float = Field(ge=0, le=100)
    visual_clarity: float = Field(ge=0, le=100)
    payoff_strength: float = Field(ge=0, le=100)
    escalation_value: float = Field(ge=0, le=100)
    commentary_opportunity: float = Field(ge=0, le=100)
    novelty: float = Field(ge=0, le=100)
    source_quality: float = Field(ge=0, le=100)
    rights_ready: bool = False


class RankedCountdownItem(BaseModel):
    candidate_id: str
    position: int = Field(ge=1)
    role: Literal["opener", "build", "false_peak", "payoff"]
    overall_score: float
    role_score: float


class RankingEpisodePlan(BaseModel):
    premise: str
    format_key: str
    format_version: str
    item_count: int
    ordered_items: list[RankedCountdownItem]


_RANKSNAXX_COUNTDOWN_V1 = RankingFormatContract(
    key="ranksnaxx_countdown",
    version="1.0.0",
    default_item_count=5,
    allowed_item_counts=(3, 5, 7),
    premise_required=True,
    commentary_required=True,
    transformative_use_required=True,
    ordering_direction="descending",
    ordering_principles=(
        "open with an immediately legible clip that earns the viewer's first second",
        "increase payoff and escalation through the middle instead of sorting randomly",
        "use number two as a credible false peak when the list is long enough",
        "reserve the strongest payoff for number one",
        "require original host framing/commentary so the episode is not a passive compilation",
    ),
    selection_dimensions=(
        "hook_strength",
        "visual_clarity",
        "payoff_strength",
        "escalation_value",
        "commentary_opportunity",
        "novelty",
        "source_quality",
        "rights_ready",
    ),
)


FORMAT_VERSIONS: dict[tuple[str, str], RankingFormatContract] = {
    (_RANKSNAXX_COUNTDOWN_V1.key, _RANKSNAXX_COUNTDOWN_V1.version): _RANKSNAXX_COUNTDOWN_V1
}


def rank_snaxx_countdown_v1() -> RankingFormatContract:
    return _RANKSNAXX_COUNTDOWN_V1.model_copy(deep=True)


def get_ranking_format(key: str, version: str) -> RankingFormatContract:
    contract = FORMAT_VERSIONS.get((key, version))
    if contract is None:
        raise KeyError(f"unknown editorial format: {key} {version}")
    return contract.model_copy(deep=True)


def _overall_score(candidate: RankingCandidateSignals) -> float:
    return (
        0.18 * candidate.hook_strength
        + 0.14 * candidate.visual_clarity
        + 0.24 * candidate.payoff_strength
        + 0.12 * candidate.escalation_value
        + 0.18 * candidate.commentary_opportunity
        + 0.08 * candidate.novelty
        + 0.06 * candidate.source_quality
    )


def _role_score(candidate: RankingCandidateSignals, role: str) -> float:
    overall = _overall_score(candidate)
    if role == "opener":
        return (
            0.40 * candidate.hook_strength
            + 0.30 * candidate.visual_clarity
            + 0.20 * candidate.commentary_opportunity
            + 0.10 * overall
        )
    if role == "payoff":
        return (
            0.55 * candidate.payoff_strength
            + 0.15 * candidate.commentary_opportunity
            + 0.15 * candidate.novelty
            + 0.15 * overall
        )
    if role == "false_peak":
        return (
            0.40 * candidate.payoff_strength
            + 0.20 * candidate.hook_strength
            + 0.20 * candidate.escalation_value
            + 0.10 * candidate.commentary_opportunity
            + 0.10 * overall
        )
    if role == "build":
        return (
            0.35 * candidate.payoff_strength
            + 0.25 * candidate.escalation_value
            + 0.15 * candidate.commentary_opportunity
            + 0.15 * candidate.novelty
            + 0.10 * overall
        )
    raise ValueError(f"unknown ranking role: {role}")


def _pick_best(
    candidates: Sequence[RankingCandidateSignals],
    *,
    role: str,
) -> RankingCandidateSignals:
    if not candidates:
        raise ValueError(f"no candidates available for ranking role: {role}")
    return max(
        candidates,
        key=lambda candidate: (
            round(_role_score(candidate, role), 8),
            round(_overall_score(candidate), 8),
            candidate.candidate_id,
        ),
    )


def _ranked_item(
    candidate: RankingCandidateSignals,
    *,
    position: int,
    role: Literal["opener", "build", "false_peak", "payoff"],
) -> RankedCountdownItem:
    return RankedCountdownItem(
        candidate_id=candidate.candidate_id,
        position=position,
        role=role,
        overall_score=round(_overall_score(candidate), 4),
        role_score=round(_role_score(candidate, role), 4),
    )


def build_ranking_episode_plan(
    candidates: Sequence[RankingCandidateSignals],
    *,
    premise: str,
    item_count: int | None = None,
    contract: RankingFormatContract | None = None,
) -> RankingEpisodePlan:
    """Build a deterministic countdown without using an LLM or mixing rights into popularity."""
    contract = contract or rank_snaxx_countdown_v1()
    normalized_premise = premise.strip()
    if contract.premise_required and not normalized_premise:
        raise ValueError("ranking episode premise cannot be empty")

    count = item_count or contract.default_item_count
    if count not in contract.allowed_item_counts:
        raise ValueError(
            f"item_count must be one of {list(contract.allowed_item_counts)} for {contract.key}"
        )

    eligible = [candidate for candidate in candidates if candidate.rights_ready]
    if len(eligible) < count:
        raise ValueError(
            f"ranking episode requires {count} rights-ready candidates; found {len(eligible)}"
        )

    remaining = list(eligible)
    selected: dict[int, tuple[RankingCandidateSignals, str]] = {}

    payoff = _pick_best(remaining, role="payoff")
    selected[1] = (payoff, "payoff")
    remaining.remove(payoff)

    opener = _pick_best(remaining, role="opener")
    selected[count] = (opener, "opener")
    remaining.remove(opener)

    if count >= 4:
        false_peak = _pick_best(remaining, role="false_peak")
        selected[2] = (false_peak, "false_peak")
        remaining.remove(false_peak)

    middle_positions = [
        position for position in range(count - 1, 1, -1) if position not in selected
    ]
    middle_candidates = sorted(
        remaining,
        key=lambda candidate: (
            round(_role_score(candidate, "build"), 8),
            round(_overall_score(candidate), 8),
            candidate.candidate_id,
        ),
        reverse=True,
    )[: len(middle_positions)]

    # The middle should grow stronger as the countdown approaches #1. Sort the selected
    # build candidates from weakest to strongest while positions descend toward #2/#1.
    middle_candidates.sort(
        key=lambda candidate: (
            round(_role_score(candidate, "build"), 8),
            round(_overall_score(candidate), 8),
            candidate.candidate_id,
        )
    )
    for position, candidate in zip(middle_positions, middle_candidates, strict=True):
        selected[position] = (candidate, "build")

    ordered_items: list[RankedCountdownItem] = []
    for position in range(count, 0, -1):
        candidate, role = selected[position]
        ordered_items.append(
            _ranked_item(
                candidate,
                position=position,
                role=role,  # type: ignore[arg-type]
            )
        )

    return RankingEpisodePlan(
        premise=normalized_premise,
        format_key=contract.key,
        format_version=contract.version,
        item_count=count,
        ordered_items=ordered_items,
    )
