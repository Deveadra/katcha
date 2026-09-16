import pytest

from katcha.branding import (
    brand_contract_for_profile_metadata,
    channel_01_brand_v1,
    rank_snaxx_brand_v1,
    validate_brand_contract,
)
from katcha.editorial.rankings import (
    RankingCandidateSignals,
    build_ranking_episode_plan,
    rank_snaxx_countdown_v1,
)


def _candidate(
    candidate_id: str,
    *,
    hook: float,
    visual: float,
    payoff: float,
    escalation: float = 60,
    commentary: float = 70,
    novelty: float = 60,
    source_quality: float = 80,
    rights_ready: bool = True,
) -> RankingCandidateSignals:
    return RankingCandidateSignals(
        candidate_id=candidate_id,
        hook_strength=hook,
        visual_clarity=visual,
        payoff_strength=payoff,
        escalation_value=escalation,
        commentary_opportunity=commentary,
        novelty=novelty,
        source_quality=source_quality,
        rights_ready=rights_ready,
    )


def test_rank_snaxx_brand_is_named_and_format_linked() -> None:
    contract = rank_snaxx_brand_v1()
    restored = validate_brand_contract(contract.model_dump(mode="json"))

    assert restored.brand_key == "ranksnaxx"
    assert restored.identity is not None
    assert restored.identity.name == "RankSnaxx"
    assert restored.identity.handle == "@ranksnaxx"
    assert restored.editorial_format is not None
    assert restored.editorial_format.key == "ranksnaxx_countdown"
    assert restored.editorial_format.default_item_count == 5


def test_profile_identity_resolves_rank_snaxx_without_breaking_legacy_default() -> None:
    resolved = brand_contract_for_profile_metadata(
        {"channel_title": "RankSnaxx", "channel_handle": "@ranksnaxx"}
    )
    legacy = brand_contract_for_profile_metadata({"channel_title": "Another Channel"})

    assert resolved.brand_key == "ranksnaxx"
    assert legacy.brand_key == channel_01_brand_v1().brand_key


def test_rank_snaxx_format_requires_premise_and_transformative_commentary() -> None:
    contract = rank_snaxx_countdown_v1()

    assert contract.allowed_item_counts == (3, 5, 7)
    assert contract.commentary_required is True
    assert contract.transformative_use_required is True
    with pytest.raises(ValueError, match="premise"):
        build_ranking_episode_plan([], premise="")


def test_countdown_reserves_opener_false_peak_and_strongest_payoff() -> None:
    candidates = [
        _candidate("best-opener", hook=99, visual=98, payoff=72),
        _candidate("finale", hook=70, visual=80, payoff=100, novelty=95),
        _candidate("false-peak", hook=88, visual=84, payoff=94, escalation=92),
        _candidate("build-a", hook=75, visual=82, payoff=78, escalation=72),
        _candidate("build-b", hook=76, visual=83, payoff=84, escalation=83),
        _candidate("extra", hook=55, visual=65, payoff=67, escalation=60),
    ]

    plan = build_ranking_episode_plan(
        candidates,
        premise="Five ridiculous recoveries that keep getting better",
    )
    positions = {item.position: item for item in plan.ordered_items}

    assert [item.position for item in plan.ordered_items] == [5, 4, 3, 2, 1]
    assert positions[5].candidate_id == "best-opener"
    assert positions[5].role == "opener"
    assert positions[2].candidate_id == "false-peak"
    assert positions[2].role == "false_peak"
    assert positions[1].candidate_id == "finale"
    assert positions[1].role == "payoff"
    assert positions[3].role == "build"
    assert positions[4].role == "build"
    assert positions[3].role_score >= positions[4].role_score


def test_rights_readiness_is_a_hard_gate_not_a_popularity_weight() -> None:
    candidates = [
        _candidate("blocked-viral", hook=100, visual=100, payoff=100, rights_ready=False),
        _candidate("a", hook=80, visual=80, payoff=80),
        _candidate("b", hook=81, visual=81, payoff=81),
        _candidate("c", hook=82, visual=82, payoff=82),
        _candidate("d", hook=83, visual=83, payoff=83),
        _candidate("e", hook=84, visual=84, payoff=84),
    ]

    plan = build_ranking_episode_plan(candidates, premise="Five escalating saves")

    assert "blocked-viral" not in {item.candidate_id for item in plan.ordered_items}


def test_insufficient_rights_ready_candidates_fail_closed() -> None:
    candidates = [
        _candidate("a", hook=80, visual=80, payoff=80),
        _candidate("b", hook=81, visual=81, payoff=81),
        _candidate("c", hook=82, visual=82, payoff=82),
        _candidate("d", hook=100, visual=100, payoff=100, rights_ready=False),
        _candidate("e", hook=100, visual=100, payoff=100, rights_ready=False),
    ]

    with pytest.raises(ValueError, match="rights-ready"):
        build_ranking_episode_plan(candidates, premise="Five escalating saves")
