from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOP_WORDS = {
    "about",
    "after",
    "again",
    "also",
    "and",
    "are",
    "but",
    "for",
    "from",
    "has",
    "have",
    "how",
    "into",
    "its",
    "new",
    "not",
    "now",
    "off",
    "out",
    "over",
    "that",
    "the",
    "their",
    "this",
    "video",
    "was",
    "what",
    "when",
    "where",
    "with",
    "you",
    "your",
}


@dataclass(frozen=True, slots=True)
class ClusterCandidate:
    candidate_id: str
    title: str
    source_key: str


@dataclass(frozen=True, slots=True)
class TrendCluster:
    cluster_key: str
    label: str
    member_ids: tuple[str, ...]
    source_keys: tuple[str, ...]
    shared_tokens: tuple[str, ...]

    @property
    def member_count(self) -> int:
        return len(self.member_ids)

    @property
    def source_count(self) -> int:
        return len(self.source_keys)


def title_tokens(title: str) -> frozenset[str]:
    return frozenset(
        token
        for token in _TOKEN_RE.findall(title.casefold())
        if len(token) >= 3 and token not in _STOP_WORDS
    )


def title_similarity(left: str, right: str) -> float:
    left_tokens = title_tokens(left)
    right_tokens = title_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    shared = left_tokens & right_tokens
    if len(shared) < 2:
        return 0.0
    union = left_tokens | right_tokens
    return len(shared) / len(union)


def _connected_components(
    candidates: list[ClusterCandidate],
    *,
    threshold: float,
) -> list[list[int]]:
    parent = list(range(len(candidates)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return
        if left_root < right_root:
            parent[right_root] = left_root
        else:
            parent[left_root] = right_root

    for left in range(len(candidates)):
        for right in range(left + 1, len(candidates)):
            if title_similarity(candidates[left].title, candidates[right].title) >= threshold:
                union(left, right)

    groups: dict[int, list[int]] = {}
    for index in range(len(candidates)):
        groups.setdefault(find(index), []).append(index)
    return [groups[key] for key in sorted(groups)]


def cluster_candidates(
    candidates: list[ClusterCandidate],
    *,
    threshold: float = 0.45,
) -> tuple[TrendCluster, ...]:
    if not 0 < threshold <= 1:
        raise ValueError("cluster threshold must be between 0 and 1")
    ordered = sorted(candidates, key=lambda item: item.candidate_id)
    clusters: list[TrendCluster] = []
    for component in _connected_components(ordered, threshold=threshold):
        members = [ordered[index] for index in component]
        member_ids = tuple(sorted(item.candidate_id for item in members))
        source_keys = tuple(sorted({item.source_key for item in members}))
        token_sets = [title_tokens(item.title) for item in members]
        shared_tokens = (
            tuple(sorted(set.intersection(*(set(tokens) for tokens in token_sets))))
            if token_sets
            else ()
        )
        representative = min(
            members,
            key=lambda item: (-len(title_tokens(item.title)), item.title.casefold()),
        )
        digest = hashlib.sha256("|".join(member_ids).encode()).hexdigest()[:24]
        clusters.append(
            TrendCluster(
                cluster_key=f"story:{digest}",
                label=representative.title,
                member_ids=member_ids,
                source_keys=source_keys,
                shared_tokens=shared_tokens,
            )
        )
    return tuple(clusters)
