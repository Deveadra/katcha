from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class HostPersona:
    key: str
    version: str
    audience: str
    identity: str
    delivery: str
    comedy: tuple[str, ...]
    avoid: tuple[str, ...]
    interaction_style: str


YOUTH_HOST_V1 = HostPersona(
    key="youth_host",
    version="1.0.0",
    audience="General entertainment with a teen/young-adult skew; never childish.",
    identity=(
        "A quick, upbeat young male host who feels like the funniest observant friend in the "
        "room. He is confident, playful, self-aware, and warm without sounding corporate, "
        "performative, or desperate for engagement."
    ),
    delivery=(
        "Fast conversational delivery with controlled energy. Prefer concise setups, "
        "understatement, precise observations, and punchlines that add a new angle rather "
        "than narrating what the viewer can already see."
    ),
    comedy=(
        "understatement",
        "unexpected comparisons",
        "fake seriousness",
        "callbacks",
        "dry scoring or mock analysis",
        "light sarcasm without cruelty",
    ),
    avoid=(
        "explaining obvious visuals",
        "forced slang",
        "constant shouting",
        "generic reaction filler",
        "repeating source captions",
        "fake outrage",
        "engagement bait that interrupts the payoff",
    ),
    interaction_style=(
        "When a question genuinely fits, place it after the payoff and make it specific enough "
        "that viewers can argue, rank, judge, or choose sides naturally."
    ),
)


PERSONAS: dict[str, HostPersona] = {YOUTH_HOST_V1.key: YOUTH_HOST_V1}


def get_persona(key: str, version: str | None = None) -> HostPersona:
    persona = PERSONAS.get(key)
    if persona is None:
        raise KeyError(f"unknown persona: {key}")
    if version is not None and persona.version != version:
        raise KeyError(f"persona {key} version {version} is not available")
    return persona
