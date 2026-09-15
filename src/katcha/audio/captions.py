from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CaptionCue:
    start_seconds: float
    end_seconds: float
    text: str


def build_caption_cues(
    text: str,
    *,
    duration_seconds: float,
    start_offset_seconds: float = 0,
    words_per_cue: int = 4,
) -> list[CaptionCue]:
    words = [word for word in text.strip().split() if word]
    if not words or duration_seconds <= 0:
        return []

    groups = [words[index : index + words_per_cue] for index in range(0, len(words), words_per_cue)]
    weights = [max(1, sum(len(word) for word in group)) for group in groups]
    total_weight = sum(weights)
    cursor = start_offset_seconds
    cues: list[CaptionCue] = []

    for index, (group, weight) in enumerate(zip(groups, weights, strict=True)):
        if index == len(groups) - 1:
            end = start_offset_seconds + duration_seconds
        else:
            share = duration_seconds * (weight / total_weight)
            end = cursor + share
        cues.append(
            CaptionCue(
                start_seconds=round(cursor, 3),
                end_seconds=round(end, 3),
                text=" ".join(group),
            )
        )
        cursor = end
    return cues
