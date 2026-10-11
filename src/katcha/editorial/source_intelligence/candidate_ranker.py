"""Deterministic receipt-backed reference ranking, never production media clearance."""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, ValidationInfo, field_validator

from katcha.acquisition.adapters import DiscoveredCandidate
from katcha.editorial.source_intelligence.candidates import (
    AssetCandidateV1,
    candidate_from_provider,
)
from katcha.editorial.source_intelligence.contracts import (
    Record,
    SearchScopeV1,
    SourceSearchJobV1,
    Text,
    digest,
)
from katcha.editorial.source_intelligence.jobs import SearchJobLedger
from katcha.editorial.source_intelligence.requirements import AssetManifestV1, AssetRequirementV1
from katcha.editorial.source_intelligence.scouting import ScoutPageV1

_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "was",
        "with",
        "select",
        "grounded",
        "permitted",
        "visual",
        "covering",
        "narration",
        "scene",
        "unknown",
    ]
)


def _terms(text: str) -> set[str]:
    return {term for term in re.findall(r"[^\W_]+", text.casefold()) if term not in _STOPWORDS}


class CoverageGapV1(Record):
    category: Text
    next_action: Annotated[str, Field(min_length=1, max_length=1000)]
    receipt_refs: tuple[Text, ...] = ()
    candidate_id: Text | None = None
    job_refs: tuple[Text, ...] = ()

    @field_validator("category", "next_action", mode="before")
    @classmethod
    def bounded_display(cls, value: str, info: ValidationInfo) -> str:
        limit = 200 if info.field_name == "category" else 1000
        text = value.strip() or "Review saved source evidence."
        return text if len(text) <= limit else text[: limit - 3] + "..."

    @field_validator("receipt_refs", "job_refs", mode="before")
    @classmethod
    def safe_diagnostic_refs(cls, value: tuple[str, ...], info: ValidationInfo) -> tuple[str, ...]:
        prefix = "ssr" if info.field_name == "receipt_refs" else "ssj"
        return tuple(key for key in value if re.fullmatch(prefix + r"_[a-f0-9]{64}", key))

    @field_validator("candidate_id", mode="before")
    @classmethod
    def safe_candidate_ref(cls, value: str | None) -> str | None:
        return value if value and re.fullmatch(r"asc_[a-f0-9]{64}", value) else None


class RankedReferenceV1(Record):
    candidate_id: Text
    canonical_url: Annotated[str, Field(max_length=4096)]
    provider: Text
    supporting_candidate_ids: tuple[Text, ...]
    supporting_providers: tuple[Text, ...]
    scout_receipt_refs: tuple[Text, ...]
    observation_receipt_refs: tuple[Text, ...]
    relevance_score: Annotated[int, Field(ge=1, le=100)]
    matched_title_terms: tuple[Annotated[str, Field(min_length=1, max_length=20000)], ...]
    matched_description_terms: tuple[Annotated[str, Field(min_length=1, max_length=20000)], ...]
    relevance_basis: Literal["script_metadata_overlap", "unverified_query_metadata_overlap"]
    explanation: Annotated[str, Field(min_length=1, max_length=1000)]
    observation_basis: Literal["provider_reported"] = "provider_reported"
    publisher_authority: Literal["unverified"] = "unverified"
    availability: Literal["reference_only"] = "reference_only"
    rights_state: Literal["not_assessed"] = "not_assessed"
    quality: None = None
    source_in_seconds: None = None
    source_out_seconds: None = None


class RequirementCoverageV1(Record):
    requirement_id: Text
    coverage_status: Literal["reference_only", "blocked", "needs_operator_input", "unresolved"]
    references: Annotated[tuple[RankedReferenceV1, ...], Field(max_length=20)]
    relevant_reference_count: Annotated[int, Field(ge=0)]
    rejected_candidate_count: Annotated[int, Field(ge=0)]
    usable_media_count: Literal[0] = 0
    gaps: tuple[CoverageGapV1, ...]
    receipt_refs: tuple[Text, ...]


class SourceCoverageReportV1(Record):
    schema_version: Literal[1] = 1
    policy_version: Literal["reference-lexical-diversity-v1"] = "reference-lexical-diversity-v1"
    scope: SearchScopeV1
    requirements: Annotated[tuple[RequirementCoverageV1, ...], Field(max_length=12000)]
    rejected_evidence: tuple[CoverageGapV1, ...]
    usable_media_count: Literal[0] = 0


def _candidate_consistent(
    candidate: AssetCandidateV1, job: SourceSearchJobV1, page: ScoutPageV1
) -> bool:
    """Reapply the accepted observation adapter instead of duplicating its hashes."""
    if candidate.original_url not in page.receipt.source_urls:
        return False
    observation = candidate.observed_metadata
    expected = candidate_from_provider(
        DiscoveredCandidate(
            source_url=candidate.original_url,
            external_id=candidate.provider_media_id,
            title=observation.title,
            creator=observation.publisher,
            creator_url=observation.publisher_url,
            metadata=dict(observation.metadata),
            provenance_claims={"discovered_via": observation.discovery_method},
        ),
        job,
        page.receipt,
    )
    return candidate == expected


def _rank_reference(
    candidate: AssetCandidateV1, requirement: AssetRequirementV1, job: SourceSearchJobV1
) -> RankedReferenceV1 | None:
    observation = candidate.observed_metadata
    title = _terms(observation.title or "")
    raw_description = observation.metadata.get("description")
    description = _terms(raw_description) if isinstance(raw_description, str) else set()
    targets = _terms(requirement.intent)
    title_hits = title & targets
    description_hits = description & targets
    basis: Literal["script_metadata_overlap", "unverified_query_metadata_overlap"] = (
        "script_metadata_overlap"
    )
    if not title_hits and not description_hits:
        targets = _terms(" ".join(job.query_terms))
        title_hits = title & targets
        description_hits = description & targets
        basis = "unverified_query_metadata_overlap"
    if not targets or not (title_hits | description_hits):
        return None
    score = max(1, (2 * len(title_hits) + len(description_hits)) * 100 // (3 * len(targets)))
    return RankedReferenceV1(
        candidate_id=candidate.candidate_id,
        canonical_url=candidate.canonical_url,
        provider=candidate.provider,
        supporting_candidate_ids=(candidate.candidate_id,),
        supporting_providers=(candidate.provider,),
        scout_receipt_refs=candidate.scout_receipts,
        observation_receipt_refs=candidate.provenance_evidence_refs,
        relevance_score=score,
        matched_title_terms=tuple(sorted(title_hits)),
        matched_description_terms=tuple(sorted(description_hits)),
        relevance_basis=basis,
        explanation=(
            "Script terms overlap reported metadata; inspect the reference for relevance."
            if basis == "script_metadata_overlap"
            else "Unverified query terms overlap reported metadata; confirm the proposed treatment."
        ),
    )


def _diverse(
    references: list[RankedReferenceV1], limit: int
) -> tuple[tuple[RankedReferenceV1, ...], int]:
    groups: dict[str, list[RankedReferenceV1]] = defaultdict(list)
    for reference in references:
        groups[reference.canonical_url].append(reference)
    unique = []
    for items in groups.values():
        items.sort(
            key=lambda item: (
                -item.relevance_score,
                item.provider,
                item.candidate_id,
                item.observation_receipt_refs,
            )
        )
        primary = items[0]
        unique.append(
            primary.model_copy(
                update={
                    "supporting_candidate_ids": tuple(
                        sorted({key for item in items for key in item.supporting_candidate_ids})
                    ),
                    "supporting_providers": tuple(sorted({item.provider for item in items})),
                    "scout_receipt_refs": tuple(
                        sorted({key for item in items for key in item.scout_receipt_refs})
                    ),
                    "observation_receipt_refs": tuple(
                        sorted({key for item in items for key in item.observation_receipt_refs})
                    ),
                }
            )
        )
    unique.sort(
        key=lambda item: (
            -item.relevance_score,
            item.provider,
            item.canonical_url,
            item.candidate_id,
        )
    )
    chosen: list[RankedReferenceV1] = []
    providers = set()
    # First expose distinct providers, then fill remaining slots by lexical relevance.
    for reference in unique:
        if reference.provider not in providers and len(chosen) < limit:
            chosen.append(reference)
            providers.add(reference.provider)
    for reference in unique:
        if reference not in chosen and len(chosen) < limit:
            chosen.append(reference)
    return tuple(chosen), len(unique)


def _gap_order(gap: CoverageGapV1) -> tuple[object, ...]:
    return (gap.category, gap.receipt_refs, gap.job_refs, gap.candidate_id or "", gap.next_action)


def rank_source_coverage(
    manifest: AssetManifestV1,
    *,
    ledger: SearchJobLedger,
    jobs: tuple[SourceSearchJobV1, ...],
    pages: tuple[ScoutPageV1, ...],
    max_references: int = 5,
) -> SourceCoverageReportV1:
    """Rank every requirement using only consistent current-revision scout evidence.

    Cross-channel/project inputs fail closed. Stale/unbacked/conflicting evidence
    becomes an actionable rejected-evidence gap; no stale links enter the report.
    All candidates remain reference-only with unassessed rights. No input changes,
    media observations, acquisition, clearance or production coverage are claimed.
    """
    if (
        isinstance(max_references, bool)
        or not isinstance(max_references, int)
        or not 1 <= max_references <= 20
    ):
        raise ValueError("reference bound must be between 1 and 20")
    if (
        len(jobs) > 10000
        or len(pages) > 10000
        or sum(len(page.candidates) for page in pages) > 100000
    ):
        raise ValueError("ranking input exceeds the bounded evidence batch")
    current_scope = manifest.scope
    for scope in (
        [job.scope for job in jobs]
        + [page.receipt.scope for page in pages]
        + [candidate.scope for page in pages for candidate in page.candidates]
    ):
        if (scope.channel_id, scope.project_id) != (
            current_scope.channel_id,
            current_scope.project_id,
        ):
            raise PermissionError("ranking evidence belongs to another channel/project")
    requirements = {item.requirement_id: item for item in manifest.requirements}
    valid_jobs = {}
    rejected = []
    for job in jobs:
        if job.scope != current_scope:
            rejected.append(
                CoverageGapV1(
                    category="stale_revision_job",
                    job_refs=(job.job_id,),
                    next_action="Load current-revision search jobs before reporting coverage.",
                )
            )
            continue
        try:
            job.verify_identity()
        except ValueError:
            rejected.append(
                CoverageGapV1(
                    category="invalid_job_identity",
                    job_refs=(job.job_id,),
                    next_action="Reload the saved search intent and verify its identity.",
                )
            )
            continue
        if (
            job.requirement_id in requirements
            and job.semantic_brief == requirements[job.requirement_id].intent
        ):
            valid_jobs[job.job_id] = job
        else:
            rejected.append(
                CoverageGapV1(
                    category="requirement_intent_mismatch",
                    job_refs=(job.job_id,),
                    next_action="Load the search job planned for this exact requirement intent.",
                )
            )
    page_groups: dict[str, list[ScoutPageV1]] = defaultdict(list)
    for page in pages:
        page_groups[page.receipt.receipt_id].append(page)
    refs: dict[str, list[RankedReferenceV1]] = defaultdict(list)
    gaps: dict[str, list[CoverageGapV1]] = defaultdict(list)
    receipt_refs: dict[str, set[str]] = defaultdict(set)
    rejected_counts: dict[str, int] = defaultdict(int)
    statuses: dict[str, list[str]] = defaultdict(list)
    for receipt_id, group in sorted(page_groups.items()):
        page = group[0]
        if any(item != page for item in group):
            rejected.append(
                CoverageGapV1(
                    category="conflicting_receipt_evidence",
                    receipt_refs=(receipt_id,),
                    next_action="Reconcile the conflicting saved scout page before ranking it.",
                )
            )
            continue
        if not re.fullmatch(r"ssr_[a-f0-9]{64}", receipt_id):
            rejected.append(
                CoverageGapV1(
                    category="invalid_receipt_identity",
                    next_action="Reload a valid saved scout receipt before ranking it.",
                )
            )
            continue
        page_job = valid_jobs.get(page.receipt.job_id)
        if page.receipt.scope != current_scope or page_job is None:
            rejected.append(
                CoverageGapV1(
                    category="stale_revision"
                    if page.receipt.scope != current_scope
                    else "unbacked_receipt",
                    receipt_refs=(receipt_id,),
                    next_action="Load current-revision jobs and their saved scout receipts.",
                )
            )
            continue
        job = page_job
        saved_receipt = ledger.read(job.job_id, scope=current_scope)
        saved_candidates = ledger.candidates(job.job_id, scope=current_scope)
        if saved_receipt != page.receipt or sorted(
            item.model_dump_json() for item in saved_candidates
        ) != sorted(item.model_dump_json() for item in page.candidates):
            rejected.append(
                CoverageGapV1(
                    category="saved_page_mismatch",
                    receipt_refs=(receipt_id,),
                    job_refs=(job.job_id,),
                    next_action="Reload the authoritative saved scout receipt and its candidates.",
                )
            )
            rejected_counts[job.requirement_id] += len(page.candidates)
            continue
        requirement = requirements[job.requirement_id]
        receipt = page.receipt
        if (
            receipt.receipt_id != "ssr_" + digest([job.job_id, receipt.attempt])
            or not 1 <= receipt.attempt <= job.max_attempts
        ):
            rejected.append(
                CoverageGapV1(
                    category="invalid_receipt_identity",
                    receipt_refs=(receipt_id,),
                    next_action="Reload the durable receipt for this exact search job.",
                )
            )
            continue
        if receipt.status == "succeeded":
            try:
                started = datetime.fromisoformat(receipt.started_at)
                finished = datetime.fromisoformat(receipt.finished_at or "")
                valid_time = started.tzinfo is not None and finished.tzinfo is not None
            except (ValueError, TypeError):
                valid_time = False
            if not valid_time:
                rejected.append(
                    CoverageGapV1(
                        category="incomplete_success_receipt",
                        receipt_refs=(receipt_id,),
                        next_action="Reload the completed receipt before ranking references.",
                    )
                )
                continue
        if receipt.status == "succeeded" and finished < started:
            gaps[job.requirement_id].append(
                CoverageGapV1(
                    category="receipt_clock_order_warning",
                    receipt_refs=(receipt_id,),
                    next_action="Clock ordering changed; retain raw receipt times for review.",
                )
            )
        statuses[job.requirement_id].append(receipt.status)
        receipt_refs[job.requirement_id].add(receipt_id)
        if receipt.status != "succeeded":
            gaps[job.requirement_id].append(
                CoverageGapV1(
                    category=receipt.error_category or receipt.status,
                    receipt_refs=(receipt_id,),
                    next_action=receipt.next_action
                    or "Reconcile this provider outcome before issuing another request.",
                )
            )
            rejected_counts[job.requirement_id] += len(page.candidates)
            continue
        if not page.candidates:
            gaps[job.requirement_id].append(
                CoverageGapV1(
                    category="legacy_metadata_not_retained"
                    if receipt.source_urls
                    else "no_matches_on_page",
                    receipt_refs=(receipt_id,),
                    next_action="Inspect saved references or search another approved source.",
                )
            )
        for candidate in page.candidates:
            try:
                consistent = candidate.scope == current_scope and _candidate_consistent(
                    candidate, job, page
                )
            except (ValueError, TypeError):
                consistent = False
            ranked = _rank_reference(candidate, requirement, job) if consistent else None
            if ranked is None:
                rejected_counts[job.requirement_id] += 1
                gaps[job.requirement_id].append(
                    CoverageGapV1(
                        category="irrelevant_metadata"
                        if consistent
                        else "inconsistent_candidate_evidence",
                        candidate_id=candidate.candidate_id,
                        receipt_refs=(receipt_id,),
                        next_action="Inspect saved metadata or search for a better reference.",
                    )
                )
            else:
                refs[job.requirement_id].append(ranked)
    rows = []
    for requirement in manifest.requirements:
        chosen, count = _diverse(refs[requirement.requirement_id], max_references)
        row_gaps = gaps[requirement.requirement_id]
        status: Literal["reference_only", "blocked", "needs_operator_input", "unresolved"]
        if requirement.origin == "coverage_placeholder":
            status = "needs_operator_input"
            row_gaps.append(
                CoverageGapV1(
                    category="visual_purpose_unknown",
                    next_action=requirement.gap_reason,
                )
            )
        elif chosen:
            status = "reference_only"
            row_gaps.append(
                CoverageGapV1(
                    category="media_and_rights_unverified",
                    next_action=(
                        "Inspect relevance and obtain permitted media "
                        "with verified rights evidence."
                    ),
                )
            )
        else:
            outcomes = statuses[requirement.requirement_id]
            status = (
                "blocked"
                if outcomes
                and all(
                    outcome in {"failed", "policy_blocked", "quota_exceeded", "rate_limited"}
                    for outcome in outcomes
                )
                else "unresolved"
            )
            row_gaps.append(
                CoverageGapV1(
                    category="no_supported_reference",
                    next_action="Search another approved source or choose an explicit alternative.",
                )
            )
        rows.append(
            RequirementCoverageV1(
                requirement_id=requirement.requirement_id,
                coverage_status=status,
                references=chosen,
                relevant_reference_count=count,
                rejected_candidate_count=rejected_counts[requirement.requirement_id],
                gaps=tuple(sorted(row_gaps, key=_gap_order)),
                receipt_refs=tuple(sorted(receipt_refs[requirement.requirement_id])),
            )
        )
    return SourceCoverageReportV1(
        scope=current_scope,
        requirements=tuple(rows),
        rejected_evidence=tuple(sorted(rejected, key=_gap_order)),
    )
