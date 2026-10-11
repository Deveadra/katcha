"""Exact-plan adapter and conservative, project-scoped requirement invalidation.

The merged script planner remains the only parser. Its revision-local identifiers
are kept as provenance; content identities let unrelated script edits retain work.
Repeated identical spans use occurrence identities. Changing their membership
is inherently ambiguous and invalidates that whole duplicate group.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, model_validator

from katcha.editorial.project_schemas import EditorialScriptSeed
from katcha.editorial.script_visual_plan import ScriptVisualPlanV1, build_script_visual_plan
from katcha.editorial.source_intelligence.contracts import Record, SearchScopeV1, Text, digest

Hash = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Kind = Literal["footage", "still", "sound", "music", "diagram", "graphic", "citation"]
Coverage = Literal["fulfilled", "reference_only", "blocked", "needs_operator_input", "unresolved"]


class AssetRequirementV1(Record):
    schema_version: Literal[1] = 1
    requirement_id: Annotated[str, Field(pattern=r"^ar_[a-f0-9]{64}$")]
    scope: SearchScopeV1
    script_span_id: Text
    planner_requirement_id: Text
    beat_id: Text | None = None
    dependency_digest: Hash
    identity_group: Hash
    intent: Annotated[str, Field(min_length=1, max_length=4000)]
    kind: Kind
    entities: Annotated[tuple[Text, ...], Field(max_length=100)] = ()
    exact_scene_if_verified: None = None
    minimum_needed_seconds: None = None
    relative_narration_slot: Annotated[int, Field(ge=0)] | None = None
    aspect: Literal["unspecified"] = "unspecified"
    importance: Literal["required"] = "required"
    approved_source_classes: Annotated[tuple[Text, ...], Field(min_length=1, max_length=20)]
    alternatives: Annotated[tuple[Text, ...], Field(max_length=20)] = ()
    lock_status: Literal["preserved_operator_script"] = "preserved_operator_script"
    coverage_status: Coverage = "unresolved"
    gap_reason: Annotated[str, Field(min_length=1, max_length=2000)]
    origin: Literal["explicit_direction", "inline_direction", "coverage_placeholder"]


class AssetManifestV1(Record):
    schema_version: Literal[1] = 1
    scope: SearchScopeV1
    plan: ScriptVisualPlanV1
    requirements: Annotated[tuple[AssetRequirementV1, ...], Field(max_length=12000)]

    @model_validator(mode="after")
    def exact_coverage(self) -> AssetManifestV1:
        if self.scope.revision_digest != self.plan.source_sha256:
            raise ValueError("manifest scope differs from preserved script")
        planner_ids = {item.id for item in self.plan.requirements}
        if (
            len(self.requirements) != len(planner_ids)
            or {item.planner_requirement_id for item in self.requirements} != planner_ids
        ):
            raise ValueError("manifest must cover every planner requirement exactly once")
        if len({item.requirement_id for item in self.requirements}) != len(self.requirements):
            raise ValueError("duplicate requirement identity")
        planner = {item.id: item for item in self.plan.requirements}
        spans = {span.id: span for span in self.plan.spans}
        for item in self.requirements:
            cue = planner[item.planner_requirement_id]
            span = spans.get(item.script_span_id)
            if (
                span is None
                or not span.start <= cue.start < cue.end <= span.end
                or (item.intent, item.origin, item.beat_id) != (cue.intent, cue.origin, cue.beat_id)
            ):
                raise ValueError("requirement provenance differs from preserved planner cue")
        if any(item.scope != self.scope for item in self.requirements):
            raise ValueError("requirement scope differs from manifest")
        return self


def build_asset_manifest(
    seed: EditorialScriptSeed,
    *,
    channel_id: UUID,
    project_id: UUID,
    approved_source_classes: tuple[str, ...] = ("owned",),
) -> AssetManifestV1:
    """Adapt all planner cues, preserving unknown semantics and exact source text.

    Source classes are supplied by the caller's policy, never inferred from a cue.
    Relative slots are narration order, not fabricated seconds or source timecodes.
    """
    plan = build_script_visual_plan(seed)
    scope = SearchScopeV1(
        channel_id=channel_id, project_id=project_id, revision_digest=plan.source_sha256
    )
    span_keys: dict[str, str] = {}
    beat_keys: dict[str, str] = {}
    beat_slots: dict[str, int] = {}
    occurrences: Counter[str] = Counter()
    for span in plan.spans:
        group = digest({"raw": span.raw, "kind": span.kind})
        occurrence = occurrences[group]
        occurrences[group] += 1
        key = digest({"group": group, "occurrence": occurrence})
        span_keys[span.id] = key
        if span.beat_id:
            beat_slots[span.beat_id] = len(beat_slots)
            beat_keys[span.beat_id] = key
    kinds: dict[str, Kind] = {
        "video": "footage",
        "image": "still",
        "audio": "sound",
        "graphic": "graphic",
        "diagram": "diagram",
        "comparison": "graphic",
        "transition": "graphic",
        "editorial_coverage": "footage",
    }
    span_starts = [span.start for span in plan.spans]
    requirements = []
    cue_occurrences: Counter[str] = Counter()
    for item in plan.requirements:
        span = plan.spans[bisect_right(span_starts, item.start) - 1]
        # Cue position within its exact containing span distinguishes inline twins.
        dependency = digest(
            {
                "span": span_keys[span.id],
                "beat": beat_keys.get(item.beat_id or ""),
                "start": item.start - span.start,
                "end": item.end - span.start,
                "kind": item.kind,
                "origin": item.origin,
                "intent": item.intent,
                "source_classes": approved_source_classes,
            }
        )
        group = digest(
            {
                "raw": span.raw,
                "kind": item.kind,
                "origin": item.origin,
                "intent": item.intent,
            }
        )
        occurrence = cue_occurrences[group]
        cue_occurrences[group] += 1
        identity = digest(
            {
                "channel": str(channel_id),
                "project": str(project_id),
                "dependency": dependency,
                "occurrence": occurrence,
            }
        )
        ambiguous = item.origin == "coverage_placeholder"
        kind = kinds[item.kind]
        # Refine the planner's audio category using only its preserved explicit label.
        label = plan.source_text[item.start : item.end].lstrip(" [(\t").split(":", 1)[0]
        if item.kind == "audio" and label.upper() == "MUSIC":
            kind = "music"
        requirements.append(
            AssetRequirementV1(
                requirement_id="ar_" + identity,
                scope=scope,
                script_span_id=span.id,
                planner_requirement_id=item.id,
                beat_id=item.beat_id,
                dependency_digest=dependency,
                identity_group=group,
                intent=item.intent,
                kind=kind,
                relative_narration_slot=beat_slots.get(item.beat_id or ""),
                approved_source_classes=approved_source_classes,
                coverage_status="needs_operator_input" if ambiguous else "unresolved",
                gap_reason=(
                    "Choose a visual purpose for this narration; "
                    "the script supplies no verified scene."
                    if ambiguous
                    else "Find grounded source evidence; this script cue proves no scene, "
                    "timing or reuse rights."
                ),
                origin=item.origin,
            )
        )
    return AssetManifestV1(scope=scope, plan=plan, requirements=tuple(requirements))


class RequirementDeltaV1(Record):
    schema_version: Literal[1] = 1
    retained_requirement_ids: tuple[str, ...]
    invalidated_requirement_ids: tuple[str, ...]
    added_requirement_ids: tuple[str, ...]


def reconcile_requirements(
    previous: AssetManifestV1, current: AssetManifestV1
) -> RequirementDeltaV1:
    """Return invalidations only; never delete bindings, annotations or pinned choices.

    Consumers retain user work and mark dependent searches/bindings stale. Duplicate
    groups whose membership changes are ambiguous: invalidate all earlier members.
    """
    if (previous.scope.channel_id, previous.scope.project_id) != (
        current.scope.channel_id,
        current.scope.project_id,
    ):
        raise ValueError("cannot reconcile requirements across channel/project scope")
    old = {item.requirement_id for item in previous.requirements}
    new = {item.requirement_id for item in current.requirements}
    old_counts = Counter(item.identity_group for item in previous.requirements)
    new_counts = Counter(item.identity_group for item in current.requirements)
    ambiguous_groups = {
        group
        for group in old_counts.keys() | new_counts.keys()
        if max(old_counts[group], new_counts[group]) > 1 and old_counts[group] != new_counts[group]
    }
    invalid = (old - new) | {
        item.requirement_id
        for item in previous.requirements
        if item.identity_group in ambiguous_groups
    }
    return RequirementDeltaV1(
        retained_requirement_ids=tuple(sorted((old & new) - invalid)),
        invalidated_requirement_ids=tuple(sorted(invalid)),
        added_requirement_ids=tuple(sorted(new - ((old & new) - invalid))),
    )


def stale_dependent_records(
    delta: RequirementDeltaV1, dependencies: dict[str, tuple[str, ...]]
) -> tuple[str, ...]:
    """Identify records using invalidated requirements without mutating any record."""
    invalid = set(delta.invalidated_requirement_ids)
    return tuple(sorted(key for key, refs in dependencies.items() if invalid.intersection(refs)))
