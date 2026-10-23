"""Evaluation contract fixtures are not claims of video understanding."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from katcha.editorial.semantic_benchmark import (
    BenchmarkManifest,
    BenchmarkPredictions,
    evaluate_semantics,
)

FIXTURES = Path(__file__).parent / "fixtures/semantic_benchmark"


@pytest.fixture
def manifest():
    return BenchmarkManifest.model_validate_json((FIXTURES / "manifest.json").read_bytes())


def run_for(manifest, **changes):
    predictions = []
    for case in manifest.cases:
        candidates = []
        if case.targets:
            candidates = [
                {
                    **case.targets[0].model_dump(mode="json"),
                    "state": "verified_temporal_match",
                    "evidence_refs": ["fixture:only"],
                }
            ]
        predictions.append(
            {
                "case_id": case.case_id,
                "decision": "returned" if candidates else "abstain",
                "candidates": candidates,
                "latency_ms": 10,
                "cost_usd": None,
            }
        )
    return {
        "manifest_sha256": manifest.sha256,
        "split": "holdout",
        "provider": "fixture",
        "model_version": "contract-only-v1",
        "policy_version": "evaluation-v1",
        "execution": "fixture",
        "predictions": predictions,
        **changes,
    }


def score(manifest, raw):
    return evaluate_semantics(manifest, BenchmarkPredictions.model_validate(raw))


def test_perfect_fixture_never_claims_live_acceptance(manifest):
    result = score(manifest, run_for(manifest))
    overall = result["overall"]
    assert overall["answerable_recall_at_5"]["value"] == 1
    assert overall["verified_relation_precision"]["value"] == 1
    assert overall["negative_abstention"]["value"] == 1
    assert overall["false_verified_claims"] == 0
    assert 0 < overall["verified_relation_precision"]["wilson_95"][0] < 0.5
    assert result["production_acceptance"] == "not_assessed"
    assert result["evidence_tier"] == "fixture"
    assert result["unknown_cost_cases"] == len(manifest.cases)
    assert result["known_cost_usd"] == 0
    assert "channel:FORESCENE" in result["slices"]
    assert "channel:RankSnaxx" in result["slices"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("subject", "lookalike"),
        ("action", "catch"),
        ("object", "decoy"),
        ("asset_id", "other-episode"),
    ],
)
def test_wrong_relation_or_source_is_false_verified_claim(manifest, field, value):
    raw = run_for(manifest)
    candidate = raw["predictions"][0]["candidates"][0]
    candidate[field] = value
    if field == "asset_id":
        candidate["clip_sha256"] = "b" * 64
    result = score(manifest, raw)["overall"]
    assert result["false_verified_claims"] == 1
    assert result["answerable_top1_precision"]["value"] == 0.5


def test_high_recall_does_not_hide_wrong_top1_or_false_verification(manifest):
    raw = run_for(manifest)
    correct = raw["predictions"][0]["candidates"][0]
    wrong = {**correct, "start_tick": 1, "end_tick": 5, "subject": "lookalike"}
    raw["predictions"][0]["candidates"].insert(0, wrong)
    result = score(manifest, raw)["overall"]
    assert result["answerable_recall_at_5"]["value"] == 1
    assert result["answerable_top1_precision"]["value"] == 0.5
    assert result["false_verified_claims"] == 1


def test_hypothesis_can_be_retrieved_without_being_verified(manifest):
    raw = run_for(manifest)
    for row in raw["predictions"]:
        for candidate in row["candidates"]:
            candidate["state"] = "hypothesis_only"
    result = score(manifest, raw)["overall"]
    assert result["answerable_recall_at_5"]["value"] == 1
    assert result["verified_relation_precision"]["count"] == 0
    assert result["verified_relation_precision"]["value"] is None
    assert result["verified_relation_precision"]["wilson_95"] is None


def test_negative_assertion_is_counted_even_when_other_queries_are_perfect(manifest):
    raw = run_for(manifest)
    negative = raw["predictions"][2]
    negative["decision"] = "returned"
    negative["candidates"] = copy.deepcopy(raw["predictions"][0]["candidates"])
    result = score(manifest, raw)["overall"]
    assert result["false_verified_claims"] == 1
    assert result["negative_abstention"]["value"] < 1


def test_source_relative_ticks_use_rational_timebase_not_origin(manifest):
    raw = run_for(manifest)
    candidate = raw["predictions"][0]["candidates"][0]
    candidate["start_tick"] += 1
    candidate["end_tick"] += 1
    result = score(manifest, raw)["overall"]
    assert manifest.assets[0].source_origin_tick == 5000
    assert result["top1_boundary_within_0_5_seconds"]["value"] == 1
    assert 0.7 < result["top1_mean_iou"] < 1


@pytest.mark.parametrize("mutation", ["missing", "extra", "duplicate", "digest", "other_split"])
def test_incomplete_or_changed_run_cannot_inflate_results(manifest, mutation):
    raw = run_for(manifest)
    if mutation == "missing":
        raw["predictions"].pop()
    elif mutation == "extra":
        raw["predictions"].append({**raw["predictions"][0], "case_id": "unknown"})
    elif mutation == "duplicate":
        raw["predictions"].append(raw["predictions"][0])
    elif mutation == "digest":
        raw["manifest_sha256"] = "0" * 64
    else:
        raw["split"] = "validation"
    with pytest.raises(ValueError):
        score(manifest, raw)


@pytest.mark.parametrize(
    "field,value", [("asset_id", "unknown"), ("clip_sha256", "e" * 64), ("end_tick", 10001)]
)
def test_provider_cannot_invent_assets_hashes_or_times(manifest, field, value):
    raw = run_for(manifest)
    raw["predictions"][0]["candidates"][0][field] = value
    with pytest.raises(ValueError):
        score(manifest, raw)


@pytest.mark.parametrize("attribute", ["source_title_id", "clip_sha256"])
def test_cross_title_and_duplicate_byte_leakage_are_rejected(manifest, attribute):
    data = manifest.model_dump(mode="json")
    asset = {**data["assets"][0], "asset_id": "leak", "split": "development"}
    if attribute == "source_title_id":
        asset["clip_sha256"] = "e" * 64
    else:
        asset["source_title_id"] = "different-title"
    data["assets"].append(asset)
    with pytest.raises(ValidationError, match="leak"):
        BenchmarkManifest.model_validate(data)


@pytest.mark.parametrize(
    "mutation", ["nan", "no_evidence", "duplicate_interval", "abstain_verified"]
)
def test_invalid_provider_reports_fail_closed(manifest, mutation):
    raw = run_for(manifest)
    prediction = raw["predictions"][0]
    if mutation == "nan":
        prediction["latency_ms"] = float("nan")
    elif mutation == "no_evidence":
        prediction["candidates"][0]["evidence_refs"] = []
    elif mutation == "duplicate_interval":
        prediction["candidates"].append(prediction["candidates"][0])
    else:
        prediction["decision"] = "abstain"
    with pytest.raises(ValidationError):
        score(manifest, raw)


def test_locked_fixture_and_cli(manifest, tmp_path):
    locked = (FIXTURES / "manifest.sha256").read_text().strip()
    assert locked == manifest.sha256
    predictions = tmp_path / "predictions.json"
    predictions.write_text(json.dumps(run_for(manifest)))
    command = [
        sys.executable,
        "scripts/evaluate_semantic_benchmark.py",
        "--manifest",
        str(FIXTURES / "manifest.json"),
        "--manifest-sha256",
        locked,
        "--predictions",
        str(predictions),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    assert json.loads(result.stdout)["production_acceptance"] == "not_assessed"
    predictions.write_text('{"unexpected":"credential-that-must-not-appear"}')
    failed = subprocess.run(command, capture_output=True, text=True)
    assert failed.returncode == 2
    assert "credential-that-must-not-appear" not in failed.stderr + failed.stdout
