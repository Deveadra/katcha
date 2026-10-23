"""Offline evaluation of retrieval and verification; never authorizes media use.

Inputs are exported observations, not executable prompts or provider calls. Source
titles and byte identities cannot cross splits. Missing predictions are errors,
not implicit abstentions that could inflate hard-negative scores.
"""

from __future__ import annotations

import math
from collections import defaultdict
from fractions import Fraction
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from katcha.editorial.project_schemas import Sha256
from katcha.editorial.source_intelligence.contracts import digest

Identity = Annotated[str, Field(min_length=1, max_length=160)]
Split = Literal["development", "validation", "holdout"]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class BenchmarkAsset(Record):
    asset_id: Identity
    source_title_id: Identity
    clip_sha256: Sha256
    split: Split
    duration_ticks: int = Field(gt=0)
    timebase_numerator: int = Field(gt=0)
    timebase_denominator: int = Field(gt=0)
    source_origin_tick: int = 0
    permission_evidence_refs: tuple[Identity, ...] = Field(min_length=1, max_length=16)


class SemanticInterval(Record):
    asset_id: Identity
    clip_sha256: Sha256
    # Normalized source-relative ticks, as in SceneIndexV1; origin is not added.
    start_tick: int = Field(ge=0)
    end_tick: int = Field(gt=0)
    subject: Identity
    action: Identity
    object: Identity

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.end_tick <= self.start_tick:
            raise ValueError("Interval must have positive duration")
        return self


class BenchmarkCase(Record):
    case_id: Identity
    channel: Identity
    category: Identity
    query: str = Field(min_length=1, max_length=2000)
    split: Split
    asset_scope: tuple[Identity, ...] = Field(min_length=1, max_length=100)
    truth: Literal["answerable", "absent", "ambiguous", "no_access"]
    targets: tuple[SemanticInterval, ...] = Field(default=(), max_length=32)
    annotation_refs: tuple[Identity, ...] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def answer_consistent(self) -> Self:
        if (self.truth == "answerable") != bool(self.targets):
            raise ValueError("Only answerable cases must have observed target intervals")
        if len(set(self.asset_scope)) != len(self.asset_scope):
            raise ValueError("Asset scope contains duplicates")
        return self


class BenchmarkManifest(Record):
    schema_version: Literal["SemanticBenchmarkV1"] = "SemanticBenchmarkV1"
    benchmark_id: Identity
    evidence_tier: Literal["fixture", "independently_annotated_media"]
    assets: tuple[BenchmarkAsset, ...] = Field(min_length=1, max_length=10000)
    cases: tuple[BenchmarkCase, ...] = Field(min_length=1, max_length=10000)

    @model_validator(mode="after")
    def scope_and_split_integrity(self) -> Self:
        assets = {item.asset_id: item for item in self.assets}
        if len(assets) != len(self.assets) or len({c.case_id for c in self.cases}) != len(
            self.cases
        ):
            raise ValueError("Duplicate asset or case identity")
        for attribute in ("source_title_id", "clip_sha256"):
            splits: dict[str, str] = {}
            for asset in self.assets:
                key = getattr(asset, attribute)
                if key in splits and splits[key] != asset.split:
                    raise ValueError("Source title or media bytes leak across benchmark splits")
                splits[key] = asset.split
        for case in self.cases:
            if any(
                key not in assets or assets[key].split != case.split for key in case.asset_scope
            ):
                raise ValueError("Case scope must contain known assets from its own split")
            for target in case.targets:
                validate_interval(target, assets, set(case.asset_scope))
        return self

    @property
    def sha256(self) -> str:
        return digest(self.model_dump(mode="json"))


class RankedObservation(SemanticInterval):
    state: Literal["retrieved", "hypothesis_only", "verified_temporal_match"]
    evidence_refs: tuple[Identity, ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def assertion_has_receipts(self) -> Self:
        if self.state == "verified_temporal_match" and not self.evidence_refs:
            raise ValueError("Verified assertions require evidence references")
        return self


class CasePrediction(Record):
    case_id: Identity
    decision: Literal["returned", "abstain"]
    candidates: tuple[RankedObservation, ...] = Field(default=(), max_length=50)
    latency_ms: float = Field(ge=0)
    cost_usd: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def decision_consistent(self) -> Self:
        if self.decision == "returned" and not self.candidates:
            raise ValueError("Returned predictions require candidates")
        if self.decision == "abstain" and any(
            item.state == "verified_temporal_match" for item in self.candidates
        ):
            raise ValueError("Abstention cannot include verified assertions")
        keys = [(c.asset_id, c.start_tick, c.end_tick) for c in self.candidates]
        if len(keys) != len(set(keys)):
            raise ValueError("Duplicate ranked interval")
        return self


class BenchmarkPredictions(Record):
    schema_version: Literal["SemanticPredictionsV1"] = "SemanticPredictionsV1"
    manifest_sha256: Sha256
    split: Split
    provider: Identity
    model_version: Identity
    policy_version: Identity
    execution: Literal["fixture", "offline_export", "live_provider_export"]
    predictions: tuple[CasePrediction, ...] = Field(min_length=1, max_length=10000)


def validate_interval(
    interval: SemanticInterval, assets: dict[str, BenchmarkAsset], scope: set[str]
) -> None:
    asset = assets.get(interval.asset_id)
    if asset is None or interval.asset_id not in scope:
        raise ValueError("Interval uses an unknown or out-of-scope asset")
    if interval.clip_sha256 != asset.clip_sha256 or interval.end_tick > asset.duration_ticks:
        raise ValueError("Interval does not match measured media identity or bounds")


def _overlap(candidate: SemanticInterval, target: SemanticInterval) -> tuple[float, bool]:
    if (
        candidate.asset_id,
        candidate.clip_sha256,
        candidate.subject,
        candidate.action,
        candidate.object,
    ) != (target.asset_id, target.clip_sha256, target.subject, target.action, target.object):
        return 0.0, False
    intersection = max(
        0, min(candidate.end_tick, target.end_tick) - max(candidate.start_tick, target.start_tick)
    )
    union = max(candidate.end_tick, target.end_tick) - min(candidate.start_tick, target.start_tick)
    return intersection / union, True


def _rate(successes: int, count: int) -> dict:
    # Wilson 95% intervals keep tiny perfect samples visibly uncertain.
    if count == 0:
        return {"successes": 0, "count": 0, "value": None, "wilson_95": None}
    value, z = successes / count, 1.959963984540054
    divisor = 1 + z * z / count
    center = (value + z * z / (2 * count)) / divisor
    radius = z * math.sqrt(value * (1 - value) / count + z * z / (4 * count**2)) / divisor
    return {
        "successes": successes,
        "count": count,
        "value": value,
        "wilson_95": [max(0.0, center - radius), min(1.0, center + radius)],
    }


def evaluate_semantics(manifest: BenchmarkManifest, run: BenchmarkPredictions) -> dict:
    """Score a complete frozen split. Results are measurements, never promotion gates."""
    if run.manifest_sha256 != manifest.sha256:
        raise ValueError("Predictions do not match the frozen benchmark digest")
    cases = {c.case_id: c for c in manifest.cases if c.split == run.split}
    predictions = {p.case_id: p for p in run.predictions}
    if len(predictions) != len(run.predictions) or set(predictions) != set(cases):
        raise ValueError("Predictions must cover every case in exactly one split exactly once")
    assets = {asset.asset_id: asset for asset in manifest.assets}
    rows = []
    for key, case in cases.items():
        prediction = predictions[key]
        for candidate in prediction.candidates:
            validate_interval(candidate, assets, set(case.asset_scope))

        def correct(candidate: SemanticInterval, targets=case.targets) -> bool:
            return any(_overlap(candidate, target)[0] >= 0.7 for target in targets)

        verified = [c for c in prediction.candidates if c.state == "verified_temporal_match"]
        top = prediction.candidates[0] if prediction.candidates else None
        localized = False
        iou = None
        if top:
            matching = [t for t in case.targets if _overlap(top, t)[1]]
            if matching:
                target = max(matching, key=lambda t: _overlap(top, t)[0])
                iou = _overlap(top, target)[0]
                asset = assets[top.asset_id]
                scale = Fraction(asset.timebase_numerator, asset.timebase_denominator)
                localized = max(
                    abs(top.start_tick - target.start_tick), abs(top.end_tick - target.end_tick)
                ) * scale <= Fraction(1, 2)
        rows.append(
            {
                "case_id": key,
                "channel": case.channel,
                "category": case.category,
                "answerable": case.truth == "answerable",
                "top1": bool(top and correct(top)),
                "top5": any(correct(c) for c in prediction.candidates[:5]),
                "verified_count": len(verified),
                "verified_correct": sum(correct(c) for c in verified),
                "abstained": prediction.decision == "abstain",
                "localized": localized,
                "top1_iou": iou,
            }
        )

    def summarize(selected: list[dict]) -> dict:
        answerable = [r for r in selected if r["answerable"]]
        negative = [r for r in selected if not r["answerable"]]
        count = sum(r["verified_count"] for r in selected)
        correct = sum(r["verified_correct"] for r in selected)
        ious = [r["top1_iou"] for r in answerable if r["top1_iou"] is not None]
        return {
            "cases": len(selected),
            "answerable_top1_precision": _rate(sum(r["top1"] for r in answerable), len(answerable)),
            "answerable_recall_at_5": _rate(sum(r["top5"] for r in answerable), len(answerable)),
            "verified_relation_precision": _rate(correct, count),
            "false_verified_claims": count - correct,
            "negative_abstention": _rate(sum(r["abstained"] for r in negative), len(negative)),
            "top1_boundary_within_0_5_seconds": _rate(
                sum(r["localized"] for r in answerable), len(answerable)
            ),
            "top1_mean_iou": sum(ious) / len(ious) if ious else None,
            "top1_iou_sample_count": len(ious),
        }

    slices: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        slices[f"channel:{row['channel']}"].append(row)
        slices[f"category:{row['category']}"].append(row)
    costs = [p.cost_usd for p in run.predictions]
    return {
        "schema_version": "SemanticBenchmarkReportV1",
        "manifest_sha256": manifest.sha256,
        "predictions_sha256": digest(run.model_dump(mode="json")),
        "split": run.split,
        "evidence_tier": manifest.evidence_tier,
        "execution": run.execution,
        "provider": run.provider,
        "model_version": run.model_version,
        "policy_version": run.policy_version,
        "production_acceptance": "not_assessed",
        "limitations": [
            "Scores do not validate receipt contents, licensing or provider identity.",
            "Fixture scores cannot establish real semantic capability.",
            "Production admission requires independently reviewed live evidence.",
        ],
        "overall": summarize(rows),
        "slices": {key: summarize(value) for key, value in sorted(slices.items())},
        "cases": rows,
        "latency_ms": sum(p.latency_ms for p in run.predictions),
        "known_cost_usd": sum(c for c in costs if c is not None),
        "unknown_cost_cases": sum(c is None for c in costs),
    }
