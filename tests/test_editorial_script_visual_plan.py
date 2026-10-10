"""No-cost script-to-visual-plan coverage and authorization regression tests."""

from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError
from test_editorial_projects import brief, root, saved  # noqa: F401 - registered pytest fixture

from katcha.editorial.project_schemas import EditorialScriptSeed
from katcha.editorial.script_visual_plan import (
    ScriptVisualPlanV1,
    build_script_visual_plan,
)


def plan(text: str):
    return build_script_visual_plan(
        EditorialScriptSeed(
            text=text,
            origin="operator_paste",
            content_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )
    )


def test_preserves_every_original_character_and_recognizes_cues():
    source = (
        "## VisionQuest\r\n\r\n"
        "[VISUAL: White Vision at the window]\r\n"
        "Vision is back. [GRAPHIC: vision family tree]\r\n"
        "\r\n"
        "TRANSITION: Fade out\r\n"
        "But Ultron? He's an open question.\r\n"
        "Actual dialogue: This is not a production direction.\r\n"
    )
    result = plan(source)
    assert result.source_text == source
    assert "".join(span.raw for span in result.spans) == source
    assert [(span.start, span.end) for span in result.spans] == [
        (0 if index == 0 else result.spans[index - 1].end, span.end)
        for index, span in enumerate(result.spans)
    ]
    assert result.narration_beat_count == 2
    assert result.explicit_direction_count == 3
    assert len(result.requirements) == 5
    assert {item.kind for item in result.requirements} == {
        "video", "graphic", "transition", "editorial_coverage"
    }
    first_beat = next(span.beat_id for span in result.spans if span.beat_id)
    visual = next(req for req in result.requirements if req.intent == "White Vision at the window")
    assert visual.beat_id == first_beat
    spoken = [span.spoken_text for span in result.spans if span.beat_id]
    assert spoken[0] == "Vision is back."
    assert "[GRAPHIC:" in result.source_text
    assert result.readiness == "analysis_only_review_required"
    assert all(req.rights_status == "not_assessed" for req in result.requirements)
    assert all(req.source_time_seconds is None for req in result.requirements)


def test_many_requirements_are_never_silently_truncated_at_legacy_scout_limit():
    source = "\n\n".join(
        f"[VISUAL: evidence comparison number {number}]\nDiscuss detail {number}."
        for number in range(70)
    )
    result = plan(source)
    assert result.narration_beat_count == 70
    assert result.explicit_direction_count == 70
    assert len(result.requirements) == 140
    assert len({item.id for item in result.requirements}) == 140
    assert len({item.beat_id for item in result.requirements}) == 70
    assert result.model_dump_json() == plan(source).model_dump_json()


def test_only_explicit_directives_are_classified_not_dialogue_or_guesswork():
    result = plan(
        "Ultron: I am not dead.\n"
        "A [theory] isn't footage.\n"
        "See 01:23? That's a script reference, not an observed timestamp.\n"
        "(SHOW: old Age of Ultron trailer scene)\n"
    )
    assert result.narration_beat_count == 1
    assert result.explicit_direction_count == 1  # Parenthetical cue is explicit, but inline.
    assert len(result.requirements) == 2
    assert result.requirements[1].kind == "video"
    assert result.requirements[1].origin == "inline_direction"
    assert all(req.source_time_seconds is None for req in result.requirements)
    assert "Ultron: I am not dead." in result.spans[0].raw


def test_pure_cue_script_does_not_invent_narration_or_video():
    result = plan("[DIAGRAM: family tree]\n[MUSIC: sting]\n")
    assert result.narration_beat_count == 0
    assert result.explicit_direction_count == 2
    assert len(result.requirements) == 2
    assert all(req.beat_id is None for req in result.requirements)
    assert "No narration text was detected" in result.gaps[-1]


def test_different_source_invalidates_ids_and_rejects_corrupt_span():
    first = plan("Narration line.")
    updated = plan("Narration line!")
    assert first.source_sha256 != updated.source_sha256
    assert first.spans[0].beat_id != updated.spans[0].beat_id
    bad = first.model_dump()
    bad["spans"][0]["raw"] = "Invented scenes"
    with pytest.raises(ValidationError, match="exact original script"):
        ScriptVisualPlanV1.model_validate(bad)
    bad = first.model_dump()
    bad["requirements"][0]["beat_id"] = "beat_" + "f" * 24
    with pytest.raises(ValidationError, match="unknown beat"):
        ScriptVisualPlanV1.model_validate(bad)


def test_imported_project_exposes_read_only_visual_plan_under_channel_scope(request):
    client, channel, other = request.getfixturevalue("saved")
    source = "# Trailer breakdown\n\n[CLIP: trailer opens]\nFirst narration line.\n"
    create = brief("script-plan-import")
    create["brief"]["script_seed"] = {
        "text": source,
        "origin": "operator_paste",
        "content_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "media_type": "text/plain",
    }
    made = client.post(root(channel), json=create)
    assert made.status_code == 201, made.text
    project_id = made.json()["id"]
    url = f"{root(channel)}/{project_id}/script-visual-plan"
    response = client.get(url)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "private, no-store"
    result = response.json()
    assert result["source_text"] == source
    assert result["narration_beat_count"] == 1
    assert result["requirements"][0]["origin"] == "explicit_direction"
    assert client.get(
        url, headers={"Authorization": "Bearer editorial-reader-token-00001"}
    ).status_code == 200
    assert client.get(f"{root(other)}/{project_id}/script-visual-plan").status_code == 403
    assert client.get(f"{root(channel)}/{project_id}").json()["revision"] == 0


def test_endpoint_reports_missing_seed_as_explicit_blocker(request):
    client, channel, _ = request.getfixturevalue("saved")
    made = client.post(root(channel), json=brief("no-visual-seed"))
    assert made.status_code == 201, made.text
    response = client.get(f"{root(channel)}/{made.json()['id']}/script-visual-plan")
    assert response.status_code == 409
    assert "Import a script seed" in response.text
