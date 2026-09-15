from decimal import Decimal

from katcha.ai.pricing import estimate_token_cost
from katcha.ai.router import ModelTarget


def test_luna_cost_estimate_uses_public_paid_rate() -> None:
    target = ModelTarget("openai", "gpt-5.6-luna")
    cost = estimate_token_cost(target, input_tokens=1_000_000, output_tokens=1_000_000)
    assert cost == Decimal("1.40000000")


def test_unknown_model_costs_zero_until_pricing_is_registered() -> None:
    target = ModelTarget("example", "future-model")
    assert estimate_token_cost(target, 1000, 1000) == Decimal("0")
