from __future__ import annotations

from dataclasses import dataclass
from math import inf


@dataclass(frozen=True, slots=True)
class SimilarityMatch:
    clip_id: str
    mean_distance: float


def average_hamming_distance(left: list[str], right: list[str]) -> float:
    count = min(len(left), len(right))
    if count == 0:
        return inf
    distances: list[int] = []
    for left_hash, right_hash in zip(left[:count], right[:count], strict=True):
        try:
            distances.append((int(left_hash, 16) ^ int(right_hash, 16)).bit_count())
        except ValueError:
            return inf
    return sum(distances) / len(distances)


def rank_similar_hashes(
    target_hashes: list[str],
    candidates: list[tuple[str, list[str]]],
    *,
    max_mean_distance: float = 10.0,
    limit: int = 5,
) -> list[SimilarityMatch]:
    matches: list[SimilarityMatch] = []
    for clip_id, hashes in candidates:
        distance = average_hamming_distance(target_hashes, hashes)
        if distance <= max_mean_distance:
            matches.append(SimilarityMatch(clip_id=clip_id, mean_distance=round(distance, 3)))
    matches.sort(key=lambda item: item.mean_distance)
    return matches[:limit]
