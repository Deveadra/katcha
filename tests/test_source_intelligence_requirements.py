"""Offline requirement adapter regression tests using the accepted exact planner."""

from __future__ import annotations

import hashlib
from uuid import UUID

import pytest
from pydantic import ValidationError

from katcha.editorial.project_schemas import EditorialScriptSeed
from katcha.editorial.source_intelligence.requirements import (
    AssetManifestV1,
    build_asset_manifest,
    reconcile_requirements,
    stale_dependent_records,
)

CHANNEL = UUID(int=1)
PROJECT = UUID(int=2)


def manifest(text: str, **kwargs):
    return build_asset_manifest(
        EditorialScriptSeed(
            text=text,
            origin="operator_paste",
            content_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        ),
        channel_id=CHANNEL,
        project_id=PROJECT,
        **kwargs,
    )


def test_exact_unicode_crlf_120_cues_preserved_and_every_beat_covered():
    script = "\r\n\r\n".join(
        f"[VISUAL: Evidence {number}]\r\nNarration café {number}." for number in range(120)
    )
    result = manifest(script)
    assert result.plan.source_text.encode() == script.encode()
    assert "".join(span.raw for span in result.plan.spans) == script
    assert len(result.requirements) == 240
    assert len({item.requirement_id for item in result.requirements}) == 240
    assert {item.planner_requirement_id for item in result.requirements} == {
        item.id for item in result.plan.requirements
    }
    assert len({item.beat_id for item in result.requirements}) == 120
    assert result.model_dump_json() == manifest(script).model_dump_json()
    assert all(item.exact_scene_if_verified is None for item in result.requirements)
    assert all(item.minimum_needed_seconds is None for item in result.requirements)
    assert all(item.approved_source_classes == ("owned",) for item in result.requirements)
    assert AssetManifestV1.model_validate_json(result.model_dump_json()) == result


def test_unknown_narration_remains_actionable_ambiguity_without_entities_or_timing():
    result = manifest("Ultron at 01:23? Nobody verified the scene.")
    (item,) = result.requirements
    assert item.coverage_status == "needs_operator_input"
    assert "Choose a visual purpose" in item.gap_reason
    assert item.entities == ()
    assert item.exact_scene_if_verified is None
    assert item.relative_narration_slot == 0
    assert item.lock_status == "preserved_operator_script"


def test_all_planner_kinds_adapt_without_authorizing_sources():
    script = (
        "[IMAGE: portrait]\n[DIAGRAM: timeline]\n[GRAPHIC: title]\n"
        "[COMPARISON: before and after]\n[TRANSITION: dissolve]\n"
        "[AUDIO: room tone]\n[MUSIC: sting]\n[VIDEO: established shot]\n"
    )
    result = manifest(script, approved_source_classes=("owned", "licensed_catalog"))
    assert [item.kind for item in result.requirements] == [
        "still",
        "diagram",
        "graphic",
        "graphic",
        "graphic",
        "sound",
        "music",
        "footage",
    ]
    assert all(item.beat_id is None for item in result.requirements)
    assert all(item.relative_narration_slot is None for item in result.requirements)
    assert all(item.coverage_status == "unresolved" for item in result.requirements)
    assert all("reuse rights" in item.gap_reason for item in result.requirements)


def test_unrelated_insert_keeps_content_identities_despite_new_planner_ids():
    first = manifest("[VIDEO: One]\nFirst beat.\n\n[IMAGE: Two]\nSecond beat.")
    second = manifest("New unrelated beat.\n\n" + first.plan.source_text)
    delta = reconcile_requirements(first, second)
    assert set(delta.retained_requirement_ids) == {
        item.requirement_id for item in first.requirements
    }
    assert not delta.invalidated_requirement_ids
    assert len(delta.added_requirement_ids) == 1
    assert {item.planner_requirement_id for item in first.requirements}.isdisjoint(
        {item.planner_requirement_id for item in second.requirements}
    )


def test_edit_invalidates_only_dependent_requirements_and_preserves_annotations():
    first = manifest("[VIDEO: One]\nFirst beat.\n\n[IMAGE: Two]\nSecond beat.")
    second = manifest(first.plan.source_text.replace("First beat.", "Changed first beat."))
    delta = reconcile_requirements(first, second)
    assert len(delta.invalidated_requirement_ids) == 2
    assert len(delta.retained_requirement_ids) == 2
    annotations = {"pinned": "Operator chose original shot"}
    dependencies = {
        "first-search": (first.requirements[0].requirement_id,),
        "second-binding": (first.requirements[2].requirement_id,),
        "mixed": tuple(item.requirement_id for item in first.requirements),
        "unrelated": (),
    }
    assert stale_dependent_records(delta, dependencies) == ("first-search", "mixed")
    assert annotations == {"pinned": "Operator chose original shot"}
    assert dependencies["first-search"] == (first.requirements[0].requirement_id,)


def test_inline_twins_and_duplicate_beats_have_unique_stable_identities():
    first = manifest("Same. [IMAGE: Same] [IMAGE: Same]\n\nSame. [IMAGE: Same] [IMAGE: Same]\n")
    assert len(first.requirements) == 6
    assert len({item.requirement_id for item in first.requirements}) == 6
    assert len(reconcile_requirements(first, first).retained_requirement_ids) == 6
    second = manifest("Same. [IMAGE: Same] [IMAGE: Same]\n")
    delta = reconcile_requirements(first, second)
    assert len(delta.invalidated_requirement_ids) == 6
    assert not delta.retained_requirement_ids


def test_policy_change_invalidates_work_and_cross_scope_reconciliation_rejected():
    first = manifest("[VISUAL: One]\nNarration.")
    second = manifest(first.plan.source_text, approved_source_classes=("licensed_catalog",))
    assert len(reconcile_requirements(first, second).invalidated_requirement_ids) == 2
    cross_scope = second.model_dump()
    cross_scope["scope"]["channel_id"] = UUID(int=999)
    with pytest.raises(ValidationError, match="scope differs"):
        AssetManifestV1.model_validate(cross_scope)
    other = build_asset_manifest(
        EditorialScriptSeed(
            text=first.plan.source_text,
            origin="operator_paste",
            content_sha256=first.plan.source_sha256,
        ),
        channel_id=UUID(int=999),
        project_id=PROJECT,
    )
    assert {item.requirement_id for item in first.requirements}.isdisjoint(
        {item.requirement_id for item in other.requirements}
    )
    with pytest.raises(ValueError, match="channel/project"):
        reconcile_requirements(first, other)


def test_manifest_rejects_silent_coverage_loss_wrong_revision_and_duplicate_ids():
    first = manifest("[VISUAL: One]\nNarration.")
    value = first.model_dump()
    value["requirements"] = value["requirements"][:-1]
    with pytest.raises(ValidationError, match="every planner requirement"):
        AssetManifestV1.model_validate(value)
    value = first.model_dump()
    value["scope"]["revision_digest"] = "0" * 64
    with pytest.raises(ValidationError, match="preserved script"):
        AssetManifestV1.model_validate(value)
    value = first.model_dump()
    value["requirements"][1]["requirement_id"] = value["requirements"][0]["requirement_id"]
    with pytest.raises(ValidationError, match="duplicate requirement"):
        AssetManifestV1.model_validate(value)


def test_manifest_rejects_forged_cue_provenance():
    value = manifest("[VISUAL: One]\nNarration.").model_dump()
    value["requirements"][0]["intent"] = "Invented observed scene"
    with pytest.raises(ValidationError, match="provenance differs"):
        AssetManifestV1.model_validate(value)
