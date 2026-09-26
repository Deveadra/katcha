from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from katcha.ai.router import ModelTarget


@dataclass(frozen=True, slots=True)
class TokenPrice:
    input_per_million: Decimal
    output_per_million: Decimal


PRICES: dict[tuple[str, str], TokenPrice] = {
    ("openai", "gpt-5.6-luna"): TokenPrice(Decimal("0.20"), Decimal("1.20")),
    ("openai", "gpt-5.6-terra"): TokenPrice(Decimal("2.00"), Decimal("12.00")),
    ("openai", "gpt-5.6-sol"): TokenPrice(Decimal("4.00"), Decimal("20.00")),
    ("openai", "gpt-4o-mini-tts"): TokenPrice(Decimal("0.60"), Decimal("12.00")),
    ("openai", "gpt-4o-mini-tts-2025-12-15"): TokenPrice(
        Decimal("0.60"), Decimal("12.00")
    ),
    ("gemini", "gemini-3.5-flash-lite"): TokenPrice(Decimal("0.30"), Decimal("2.50")),
    ("gemini", "gemini-3.6-flash"): TokenPrice(Decimal("0.75"), Decimal("3.75")),
    ("gemini", "gemini-3.7-flash"): TokenPrice(Decimal("0.75"), Decimal("3.75")),
    ("gemini", "gemini-3.8-flash"): TokenPrice(Decimal("0.75"), Decimal("3.75")),
    ("gemini", "gemini-3.1-flash-tts-preview"): TokenPrice(
        Decimal("1.00"), Decimal("20.00")
    ),
}


def estimate_token_cost(target: ModelTarget, input_tokens: int, output_tokens: int) -> Decimal:
    price = PRICES.get((target.provider, target.model))
    if price is None:
        return Decimal("0")
    million = Decimal("1000000")
    cost = (
        (Decimal(input_tokens) / million) * price.input_per_million
        + (Decimal(output_tokens) / million) * price.output_per_million
    )
    return cost.quantize(Decimal("0.00000001"))
