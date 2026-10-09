"""Deterministic, read-only inventory for a verified operator script seed.

The plan preserves every original character and refuses to invent facts, licensing,
source timecodes or visual observations. A later, reviewed AI analysis can enrich it.
"""

from __future__ import annotations

import hashlib
import re
from typing import Literal, Self

from pydantic import Field, model_validator

from katcha.editorial.project_schemas import Contract, EditorialScriptSeed

# Require an explicit production cue. An ordinary "Character: dialogue" is narration.
_CUE_NAMES = (
    "VISUAL", "VIDEO", "CLIP", "B-ROLL", "BROLL", "SHOT", "SHOW",
    "IMAGE", "STILL", "PHOTO", "GRAPHIC", "DIAGRAM", "FAMILY TREE",
    "RELATIONSHIP MAP", "TIMELINE", "COMPARISON", "CALLOUT",
    "ON SCREEN", "TRANSITION", "CUT TO", "SFX", "AUDIO", "MUSIC",
)
_CUE_ALTERNATIVES = "|".join(re.escape(name) for name in sorted(_CUE_NAMES, key=len, reverse=True))
_DIRECTIVE = re.compile(
    rf"^\s*(?:\[(?P<bracket>{_CUE_ALTERNATIVES})\s*:\s*(?P<bracket_body>[^\]\r\n]{{1,500}})\]"
    rf"|(?P<label>{_CUE_ALTERNATIVES})\s*:\s*(?P<body>[^\r\n]{{1,500}}))\s*$",
    re.IGNORECASE,
)
_INLINE = re.compile(
    rf"\[(?P<bracket>{_CUE_ALTERNATIVES})\s*:\s*(?P<bracket_body>[^\]\r\n]{{1,500}})\]"
    rf"|\((?P<label>{_CUE_ALTERNATIVES})\s*:\s*(?P<body>[^\)\r\n]{{1,500}})\)",
    re.IGNORECASE,
)
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+\S")
_WORD = re.compile(r"\b[\w]+(?:['’-][\w]+)*\b", re.UNICODE)


class ScriptSpanV1(Contract):
    id: str = Field(pattern=r"^sp_[0-9a-f]{24}$")
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    raw: str = Field(min_length=1, max_length=120_000)
    kind: Literal["narration", "direction", "heading", "separator"]
    beat_id: str | None = Field(default=None, pattern=r"^beat_[0-9a-f]{24}$")
    spoken_text: str | None = None

    @model_validator(mode="after")
    def bounds(self) -> Self:
        if len(self.raw) != self.end - self.start:
            raise ValueError("script span offsets do not match its original text")
        if (self.kind == "narration") != (self.beat_id is not None):
            raise ValueError("only narration spans have beat identities")
        if self.kind != "narration" and self.spoken_text is not None:
            raise ValueError("non-narration spans cannot claim spoken narration")
        return self


class VisualRequirementV1(Contract):
    id: str = Field(pattern=r"^req_[0-9a-f]{24}$")
    beat_id: str | None = Field(default=None, pattern=r"^beat_[0-9a-f]{24}$")
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    kind: Literal[
        "video", "image", "graphic", "diagram", "comparison",
        "transition", "audio", "editorial_coverage",
    ]
    intent: str = Field(min_length=1, max_length=4000)
    origin: Literal["explicit_direction", "inline_direction", "coverage_placeholder"]
    status: Literal["unresolved"] = "unresolved"
    rights_status: Literal["not_assessed"] = "not_assessed"
    source_time_seconds: None = None  # A script is never observed source footage.


class ScriptVisualPlanV1(Contract):
    version: Literal["script-visual-plan-v1"] = "script-visual-plan-v1"
    source: Literal["operator_script_seed"] = "operator_script_seed"
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_text: str = Field(min_length=1, max_length=120_000)
    spans: list[ScriptSpanV1] = Field(min_length=1, max_length=6000)
    requirements: list[VisualRequirementV1] = Field(max_length=12000)
    narration_beat_count: int = Field(ge=0)
    explicit_direction_count: int = Field(ge=0)
    estimated_spoken_seconds: float = Field(ge=0)
    timing_basis: Literal["word_count_estimate"] = "word_count_estimate"
    readiness: Literal["analysis_only_review_required"] = "analysis_only_review_required"
    gaps: list[str] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def preserve_exact_source(self) -> Self:
        if hashlib.sha256(self.source_text.encode("utf-8")).hexdigest() != self.source_sha256:
            raise ValueError("script source digest does not match original bytes")
        cursor = 0
        beats: set[str] = set()
        for span in self.spans:
            if span.start != cursor or self.source_text[span.start : span.end] != span.raw:
                raise ValueError("spans do not cover the exact original script contiguously")
            cursor = span.end
            if span.beat_id:
                if span.beat_id in beats:
                    raise ValueError("duplicate narration beat ID")
                beats.add(span.beat_id)
        if cursor != len(self.source_text) or len(beats) != self.narration_beat_count:
            raise ValueError("script coverage or narration count mismatch")
        ids: set[str] = set()
        covered = {requirement.beat_id for requirement in self.requirements}
        for requirement in self.requirements:
            if requirement.id in ids or (requirement.beat_id and requirement.beat_id not in beats):
                raise ValueError("duplicate requirement or unknown beat")
            if not 0 <= requirement.start < requirement.end <= len(self.source_text):
                raise ValueError("requirement lies outside script")
            ids.add(requirement.id)
        if not beats <= covered:
            raise ValueError("every narration beat requires at least one visual requirement")
        return self


def _stable_id(prefix: str, digest: str, start: int, end: int, kind: str) -> str:
    key = f"{digest}:{start}:{end}:{kind}".encode("utf-8")
    return prefix + hashlib.sha256(key).hexdigest()[:24]


def _kind(label: str) -> str:
    value = label.upper().replace("-", " ")
    if value in {"SFX", "AUDIO", "MUSIC"}:
        return "audio"
    if value in {"TRANSITION", "CUT TO"}:
        return "transition"
    if value in {"IMAGE", "STILL", "PHOTO"}:
        return "image"
    if value in {"DIAGRAM", "FAMILY TREE", "RELATIONSHIP MAP", "TIMELINE"}:
        return "diagram"
    if value == "COMPARISON":
        return "comparison"
    if value in {"GRAPHIC", "CALLOUT", "ON SCREEN"}:
        return "graphic"
    return "video"


def _source_sections(text: str, digest: str) -> list[ScriptSpanV1]:
    """Group physical lines without dropping whitespace, LF/CRLF or Unicode."""
    parts: list[tuple[int, int, str]] = []
    offset = 0
    for physical in text.splitlines(keepends=True):
        unlined = physical.rstrip("\r\n")
        if not unlined.strip():
            kind = "separator"
        elif _DIRECTIVE.fullmatch(unlined):
            kind = "direction"
        elif _HEADING.match(unlined):
            kind = "heading"
        else:
            kind = "narration"
        end = offset + len(physical)
        if parts and kind in {"narration", "separator"} and parts[-1][2] == kind:
            previous = parts[-1]
            parts[-1] = (previous[0], end, kind)
        else:
            parts.append((offset, end, kind))
        offset = end
    if offset != len(text):
        raise ValueError("line splitting lost source text")
    if len(parts) > 6000:
        raise ValueError("script contains too many discrete sections; keep fewer than 6000")
    sections = []
    for start, end, kind in parts:
        raw = text[start:end]
        beat_id = _stable_id("beat_", digest, start, end, "narration") if kind == "narration" else None
        spoken = _INLINE.sub("", raw).strip() if beat_id else None
        sections.append(
            ScriptSpanV1(
                id=_stable_id("sp_", digest, start, end, kind),
                start=start, end=end, raw=raw, kind=kind,
                beat_id=beat_id, spoken_text=spoken,
            )
        )
    return sections


def _nearest_beat(spans: list[ScriptSpanV1], index: int) -> str | None:
    # A standalone production cue immediately before a paragraph applies forward.
    for span in spans[index + 1 :]:
        if span.beat_id:
            return span.beat_id
    for span in reversed(spans[:index]):
        if span.beat_id:
            return span.beat_id
    return None


def build_script_visual_plan(seed: EditorialScriptSeed) -> ScriptVisualPlanV1:
    """Inventory exact text and *explicit* cues; unknown visual semantics remain gaps.

    No AI, network, storage, provider-charge, rights or media-acquisition side effects.
    """
    digest = seed.content_sha256
    spans = _source_sections(seed.text, digest)
    requirements: list[VisualRequirementV1] = []
    directions = 0
    for index, span in enumerate(spans):
        if span.kind == "narration":
            requirements.append(
                VisualRequirementV1(
                    id=_stable_id("req_", digest, span.start, span.end, "coverage"),
                    beat_id=span.beat_id,
                    start=span.start, end=span.end,
                    kind="editorial_coverage",
                    intent="Select a grounded and permitted visual covering this narration; scene unknown.",
                    origin="coverage_placeholder",
                )
            )
            for match in _INLINE.finditer(span.raw):
                label = match.group("bracket") or match.group("label")
                instruction = (match.group("bracket_body") or match.group("body")).strip()
                if not instruction:
                    continue
                start, end = span.start + match.start(), span.start + match.end()
                requirements.append(
                    VisualRequirementV1(
                        id=_stable_id("req_", digest, start, end, "inline"),
                        beat_id=span.beat_id, start=start, end=end,
                        kind=_kind(label), intent=instruction,
                        origin="inline_direction",
                    )
                )
                directions += 1
        elif span.kind == "direction":
            match = _DIRECTIVE.fullmatch(span.raw.rstrip("\r\n"))
            if match is None:
                raise ValueError("classified production direction did not parse")
            label = match.group("bracket") or match.group("label")
            instruction = (match.group("bracket_body") or match.group("body")).strip()
            requirements.append(
                VisualRequirementV1(
                    id=_stable_id("req_", digest, span.start, span.end, "directive"),
                    beat_id=_nearest_beat(spans, index),
                    start=span.start, end=span.end,
                    kind=_kind(label), intent=instruction,
                    origin="explicit_direction",
                )
            )
            directions += 1

    words = sum(len(_WORD.findall(span.spoken_text or "")) for span in spans)
    gaps = [
        "Visual coverage is a placeholder until grounded media or graphics are selected.",
        "Script directions do not prove character identity, scene contents, timestamps or reuse rights.",
        "Narration timing is an estimate until approved uploaded or generated audio is measured.",
    ]
    if not any(span.beat_id for span in spans):
        gaps.append("No narration text was detected; review headings and production directions.")
    return ScriptVisualPlanV1(
        source_sha256=digest,
        source_text=seed.text,
        spans=spans,
        requirements=requirements,
        narration_beat_count=sum(bool(span.beat_id) for span in spans),
        explicit_direction_count=directions,
        estimated_spoken_seconds=round(words * 60 / 145, 2),
        gaps=gaps,
    )
