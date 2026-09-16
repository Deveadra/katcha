from katcha.acquisition.clustering import (
    ClusterCandidate,
    cluster_candidates,
    title_similarity,
)


def test_related_titles_cluster_without_collapsing_assets() -> None:
    candidates = [
        ClusterCandidate(
            candidate_id="a",
            title="Xbox reveals Project Nova gameplay trailer",
            source_key="youtube",
        ),
        ClusterCandidate(
            candidate_id="b",
            title="Project Nova Xbox gameplay trailer revealed",
            source_key="reddit",
        ),
        ClusterCandidate(
            candidate_id="c",
            title="Studio explains Project Nova gameplay after Xbox reveal",
            source_key="rss_atom",
        ),
    ]

    clusters = cluster_candidates(candidates, threshold=0.4)

    assert len(clusters) == 1
    assert clusters[0].member_ids == ("a", "b", "c")
    assert clusters[0].source_keys == ("reddit", "rss_atom", "youtube")
    assert clusters[0].member_count == 3
    assert clusters[0].source_count == 3


def test_unrelated_titles_stay_separate() -> None:
    candidates = [
        ClusterCandidate("a", "Xbox reveals Project Nova gameplay trailer", "youtube"),
        ClusterCandidate("b", "Cat steals sandwich during family picnic", "reddit"),
    ]

    clusters = cluster_candidates(candidates)

    assert len(clusters) == 2
    assert {cluster.member_ids for cluster in clusters} == {("a",), ("b",)}


def test_similarity_requires_more_than_one_generic_overlap() -> None:
    assert title_similarity(
        "Xbox game trailer surprises fans",
        "PlayStation game release date announced",
    ) == 0.0
