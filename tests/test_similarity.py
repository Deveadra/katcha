from math import inf

from katcha.services.similarity import average_hamming_distance, rank_similar_hashes


def test_average_hamming_distance_identical_hashes_is_zero() -> None:
    hashes = ["0000000000000000", "ffffffffffffffff"]
    assert average_hamming_distance(hashes, hashes) == 0


def test_average_hamming_distance_empty_is_infinite() -> None:
    assert average_hamming_distance([], ["0"]) == inf


def test_rank_similar_hashes_filters_distant_candidates() -> None:
    target = ["0000000000000000", "0000000000000000"]
    matches = rank_similar_hashes(
        target,
        [
            ("near", ["0000000000000001", "0000000000000001"]),
            ("far", ["ffffffffffffffff", "ffffffffffffffff"]),
        ],
        max_mean_distance=4,
    )
    assert [match.clip_id for match in matches] == ["near"]
