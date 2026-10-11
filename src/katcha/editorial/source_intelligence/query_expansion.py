"""Offline, bounded semantic query planning over the accepted requirement adapter.

Hints are operator inputs, not observations made by this service. Evidence labels
retain supplied receipt references without asserting scenes, source URLs or rights.
Dispatch uses the existing search ledger/scout, whose durable identities prevent
reissuing a known page; this module never performs network or paid-provider calls.
"""

from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass
from functools import partial
from typing import Annotated, Literal

from pydantic import Field, model_validator

from katcha.editorial.source_intelligence.contracts import (
    ProviderPolicyV1,
    Record,
    SearchIntentV1,
    SearchScopeV1,
    SourceSearchJobV1,
    Text,
)
from katcha.editorial.source_intelligence.planner import SearchCapability, plan_job
from katcha.editorial.source_intelligence.requirements import AssetManifestV1, AssetRequirementV1


class QueryHintV1(Record):
    schema_version: Literal[1] = 1
    kind: Literal["entity_alias", "publisher", "alternative", "negative"]
    value: Text
    entity: Text | None = None
    basis: Literal["operator_evidence", "hypothesis"] = "hypothesis"
    evidence_refs: Annotated[tuple[Text, ...], Field(max_length=20)] = ()

    @model_validator(mode="after")
    def attribution(self) -> QueryHintV1:
        if (self.kind == "entity_alias") != (self.entity is not None):
            raise ValueError("only entity alias hints require an entity")
        if self.basis == "operator_evidence" and not self.evidence_refs:
            raise ValueError("operator evidence requires receipt references")
        return self


@dataclass(frozen=True)
class ProviderRoute:
    """Trusted composition-root configuration; source class comes from policy."""

    capability: SearchCapability
    policy: ProviderPolicyV1
    source_class: str
    max_results: int = 20


Treatment = Literal["original", "entity_alias", "publisher", "alternative"]
Basis = Literal["script_direction", "operator_evidence", "hypothesis"]


class QueryVariantV1(Record):
    schema_version: Literal[1] = 1
    intent: SearchIntentV1
    treatment: Treatment
    basis: Basis
    evidence_refs: tuple[Text, ...] = ()


class QueryGapV1(Record):
    provider_key: Text | None = None
    category: Text
    next_action: Text


class ExpandedRequirementV1(Record):
    requirement_id: Text
    known_job_ids: Annotated[tuple[Text, ...], Field(max_length=820)]
    supplied_hints: Annotated[tuple[QueryHintV1, ...], Field(max_length=20)]
    variants: Annotated[tuple[QueryVariantV1, ...], Field(max_length=41)]
    jobs: Annotated[tuple[SourceSearchJobV1, ...], Field(max_length=820)]
    gaps: Annotated[tuple[QueryGapV1, ...], Field(max_length=900)]


class QueryExpansionV1(Record):
    schema_version: Literal[1] = 1
    scope: SearchScopeV1
    requirements: Annotated[tuple[ExpandedRequirementV1, ...], Field(max_length=12000)]

    @model_validator(mode="after")
    def scoped_jobs(self) -> QueryExpansionV1:
        if len({item.requirement_id for item in self.requirements}) != len(self.requirements):
            raise ValueError("duplicate requirement expansion")
        if len(self.jobs) > 1000 or len({job.job_id for job in self.jobs}) != len(self.jobs):
            raise ValueError("duplicate or unbounded expansion jobs")
        for item in self.requirements:
            for intent in [variant.intent for variant in item.variants] + list(item.jobs):
                if intent.scope != self.scope or intent.requirement_id != item.requirement_id:
                    raise ValueError("query expansion crosses scope or requirement")
            for job in item.jobs:
                job.verify_identity()
            if set(item.known_job_ids).intersection(job.job_id for job in item.jobs):
                raise ValueError("known jobs cannot be scheduled again")
        return self

    @property
    def jobs(self) -> tuple[SourceSearchJobV1, ...]:
        return tuple(job for item in self.requirements for job in item.jobs)


def _literal_alias(value: str, _match: re.Match[str]) -> str:
    return value


def _terms(text: str) -> tuple[str, ...]:
    # Keep the full meaning in semantic_brief; split rather than truncate long cues.
    return tuple(textwrap.wrap(text, width=200, break_long_words=True, break_on_hyphens=False))


def _add_variant(
    text: str,
    treatment: Treatment,
    basis: Basis,
    refs: tuple[str, ...] = (),
    *,
    manifest: AssetManifestV1,
    requirement: AssetRequirementV1,
    negatives: tuple[str, ...],
    requested_by: str,
    gaps: list[QueryGapV1],
    variants: list[QueryVariantV1],
) -> None:
    terms = _terms(text)
    if not terms or len(terms) > 30:
        gaps.append(
            QueryGapV1(
                category="query_bound_exceeded",
                next_action="Shorten the treatment while preserving the original script.",
            )
        )
        return
    variant = QueryVariantV1(
        intent=SearchIntentV1(
            scope=manifest.scope,
            requirement_id=requirement.requirement_id,
            query_terms=terms,
            negative_terms=negatives,
            semantic_brief=requirement.intent,
            source_purpose=(
                "alternative"
                if treatment == "alternative"
                else "citation"
                if requirement.kind == "citation"
                else "exact_scene"
            ),
            requested_by=requested_by,
        ),
        treatment=treatment,
        basis=basis,
        evidence_refs=refs,
    )
    if not any(existing.intent == variant.intent for existing in variants):
        variants.append(variant)


def expand_queries(
    manifest: AssetManifestV1,
    *,
    routes: tuple[ProviderRoute, ...],
    hints: dict[str, tuple[QueryHintV1, ...]] | None = None,
    requested_by: str,
    max_jobs: int = 1000,
    known_job_ids: frozenset[str] = frozenset(),
) -> QueryExpansionV1:
    """Preserve every requirement; report policy/capability/budget gaps explicitly.

    Plans at most 1000 jobs, 20 provider routes and 20 supplied hints per requirement.
    Alternatives change the query treatment, never the original semantic brief or
    approved source classes. known_job_ids are caller-supplied durable references;
    skipped jobs remain visible and do not consume the new-job planning bound.
    The ledger still checks actual cumulative provider usage at dispatch.
    Ambiguous placeholder narration requires a supplied
    alternative before any search can be scheduled.
    """
    if not 1 <= len(routes) <= 20:
        raise ValueError("choose between 1 and 20 provider routes")
    if isinstance(max_jobs, bool) or not isinstance(max_jobs, int) or not 1 <= max_jobs <= 1000:
        raise ValueError("job bound must be between 1 and 1000")
    keys = [(route.capability.provider_key, route.capability.provider_version) for route in routes]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate provider route")
    supplied = hints or {}
    known = {item.requirement_id for item in manifest.requirements}
    if supplied.keys() - known:
        raise ValueError("query hints refer to unknown requirements")
    if any(len(items) > 20 for items in supplied.values()):
        raise ValueError("at most 20 query hints per requirement")
    if len(known_job_ids) > 100000 or any(
        not re.fullmatch(r"ssj_[a-f0-9]{64}", key) for key in known_job_ids
    ):
        raise ValueError("known job references must be bounded valid search job IDs")
    results = []
    job_count = 0
    provider_counts = {key: 0 for key in keys}
    for requirement in manifest.requirements:
        inputs = supplied.get(requirement.requirement_id, ())
        negatives = tuple(dict.fromkeys(hint.value for hint in inputs if hint.kind == "negative"))
        variants: list[QueryVariantV1] = []
        gaps: list[QueryGapV1] = []

        add_variant = partial(
            _add_variant,
            manifest=manifest,
            requirement=requirement,
            negatives=negatives,
            requested_by=requested_by,
            gaps=gaps,
            variants=variants,
        )

        if requirement.origin != "coverage_placeholder":
            add_variant(requirement.intent, "original", "script_direction")
        for hint in inputs:
            if hint.kind == "entity_alias":
                assert hint.entity is not None  # QueryHintV1 validates the alias contract.
                pattern = re.compile(r"(?<!\w)" + re.escape(hint.entity) + r"(?!\w)", re.IGNORECASE)
                if not pattern.search(requirement.intent):
                    gaps.append(
                        QueryGapV1(
                            category="alias_entity_not_in_intent",
                            next_action="Match the alias to an entity in this script cue.",
                        )
                    )
                    continue
                add_variant(
                    pattern.sub(partial(_literal_alias, hint.value), requirement.intent),
                    "entity_alias",
                    hint.basis,
                    hint.evidence_refs,
                )
            elif hint.kind == "publisher" and requirement.origin != "coverage_placeholder":
                add_variant(
                    requirement.intent + " " + hint.value,
                    "publisher",
                    hint.basis,
                    hint.evidence_refs,
                )
            elif hint.kind == "alternative":
                add_variant(hint.value, "alternative", hint.basis, hint.evidence_refs)
        for alternative in requirement.alternatives:
            add_variant(alternative, "alternative", "hypothesis")
        if not variants:
            gaps.append(
                QueryGapV1(
                    category="visual_purpose_unknown",
                    next_action="Choose a visual purpose or an alternative for this narration.",
                )
            )
        jobs: list[SourceSearchJobV1] = []
        known_refs = []
        for route in routes:
            cap = route.capability
            if route.source_class not in requirement.approved_source_classes:
                gaps.append(
                    QueryGapV1(
                        provider_key=cap.provider_key,
                        category="source_class_not_approved",
                        next_action="Choose a provider within the approved source classes.",
                    )
                )
                continue
            if cap.external and not route.policy.external_search_authorized:
                gaps.append(
                    QueryGapV1(
                        provider_key=cap.provider_key,
                        category="external_search_not_authorized",
                        next_action="Authorize this provider under the project search policy.",
                    )
                )
                continue
            if cap.potentially_paid:
                gaps.append(
                    QueryGapV1(
                        provider_key=cap.provider_key,
                        category="paid_adapter_has_no_enforced_request_ceiling",
                        next_action="Use an approved provider with an enforced request ceiling.",
                    )
                )
                continue
            for variant in variants:
                try:
                    job = plan_job(
                        variant.intent,
                        provider_key=cap.provider_key,
                        provider_version=cap.provider_version,
                        policy=route.policy,
                        capability=cap,
                        max_results=route.max_results,
                    )
                except ValueError as exc:
                    gaps.append(
                        QueryGapV1(
                            provider_key=cap.provider_key,
                            category="provider_cannot_preserve_query",
                            next_action=str(exc),
                        )
                    )
                    continue
                if job.job_id in known_job_ids:
                    known_refs.append(job.job_id)
                    continue
                if job_count >= max_jobs:
                    gaps.append(
                        QueryGapV1(
                            provider_key=cap.provider_key,
                            category="planning_budget_exhausted",
                            next_action="Schedule this requirement in another bounded batch.",
                        )
                    )
                    continue
                planned_calls = provider_counts[(cap.provider_key, cap.provider_version)]
                if (
                    planned_calls + 1 > route.policy.max_calls
                    or (planned_calls + 1) * job.max_results > route.policy.max_results
                    or (planned_calls + 1) * job.max_cost > route.policy.max_cost
                ):
                    gaps.append(
                        QueryGapV1(
                            provider_key=cap.provider_key,
                            category="provider_plan_budget_exhausted",
                            next_action="Use the approved provider call, result and cost caps.",
                        )
                    )
                    continue
                if job.job_id not in {existing.job_id for existing in jobs}:
                    jobs.append(job)
                    job_count += 1
                    provider_counts[(cap.provider_key, cap.provider_version)] += 1
        results.append(
            ExpandedRequirementV1(
                requirement_id=requirement.requirement_id,
                known_job_ids=tuple(dict.fromkeys(known_refs)),
                supplied_hints=inputs,
                variants=tuple(variants),
                jobs=tuple(jobs),
                gaps=tuple(gaps),
            )
        )
    return QueryExpansionV1(scope=manifest.scope, requirements=tuple(results))
