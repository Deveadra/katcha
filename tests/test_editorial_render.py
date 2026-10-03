"""Durable render fault boundaries using synthetic renderer receipts."""

from unittest.mock import Mock

from test_editorial_projects import saved as _saved
from test_editorial_runs import new_run
from test_editorial_visual_compiler import compile_plan, plan

from katcha.editorial import render
from katcha.rendering.client import RenderResult
from katcha.services.editorial_projects import EditorialConflict
from katcha.services.editorial_runs import control_run, get_run

saved = _saved


def setup_render(saved, monkeypatch):
    row = new_run(saved)
    manifest = compile_plan(plan())
    monkeypatch.setattr(render, "current_manifest", Mock(return_value=manifest))
    result = RenderResult(
        manifest.output_key,
        manifest.output_duration_seconds,
        {"verified": True, "composition": "Editorial"},
    )
    dispatch = Mock(return_value=result)
    recover = Mock(return_value=result)
    monkeypatch.setattr(render, "render_editorial", dispatch)
    monkeypatch.setattr(render, "recover_editorial_output", recover)
    return row, manifest, result, dispatch, recover


def test_render_completes_with_durable_review_receipts(saved, monkeypatch):
    row, manifest, _, dispatch, recover = setup_render(saved, monkeypatch)
    result = render.render_project(str(row.id), 1)
    assert result["stage"] == "render_ready_for_review"
    stored = get_run(row.channel_profile_id, row.project_id, row.id)
    assert stored.artifacts["requires_editorial_review"] is True
    assert stored.artifacts["render_manifest"]["output_key"] == manifest.output_key
    assert stored.artifacts["render_dispatch"]["attempt"] == 1
    dispatch.assert_called_once()
    recover.assert_not_called()


def test_lost_response_recovers_without_second_dispatch(saved, monkeypatch):
    row, _, _, dispatch, recover = setup_render(saved, monkeypatch)
    dispatch.side_effect = TimeoutError("uncertain response")
    assert render.render_project(str(row.id), 1)["status"] == "blocked"
    resumed = control_run(
        row.channel_profile_id, row.project_id, row.id, expected_attempt=1, cancel=False
    )
    assert render.render_project(str(row.id), resumed.attempt)["status"] == "completed"
    dispatch.assert_called_once()
    recover.assert_called_once()


def test_missing_uncertain_output_does_not_repeat_compute(saved, monkeypatch):
    row, manifest, _, dispatch, recover = setup_render(saved, monkeypatch)
    assert render.claim_dispatch(str(row.id), 1, manifest) is True
    assert render.claim_dispatch(str(row.id), 1, manifest) is False
    recover.return_value = None
    assert render.render_project(str(row.id), 1)["status"] == "blocked"
    dispatch.assert_not_called()
    assert "uncertain completion" in get_run(row.channel_profile_id, row.project_id, row.id).error


def test_revoked_rights_prevent_promotion_after_render(saved, monkeypatch):
    row, manifest, _, dispatch, _ = setup_render(saved, monkeypatch)
    monkeypatch.setattr(
        render,
        "current_manifest",
        Mock(side_effect=[manifest, EditorialConflict("clearance revoked")]),
    )
    assert render.render_project(str(row.id), 1)["status"] == "blocked"
    stored = get_run(row.channel_profile_id, row.project_id, row.id)
    assert stored.artifacts["render_result"]["metadata"]["verified"] is True
    assert stored.stage != "render_ready_for_review"
    dispatch.assert_called_once()


def test_cancel_during_render_cannot_promote_stale_result(saved, monkeypatch):
    row, _, result, dispatch, _ = setup_render(saved, monkeypatch)

    def cancel(_):
        control_run(row.channel_profile_id, row.project_id, row.id, expected_attempt=1, cancel=True)
        return result

    dispatch.side_effect = cancel
    assert render.render_project(str(row.id), 1)["status"] == "stopped"
    assert get_run(row.channel_profile_id, row.project_id, row.id).status == "cancelled"


def test_confirmed_predispatch_rejection_can_resume_after_configuration_fix(saved, monkeypatch):
    from katcha.rendering.client import EditorialRenderRejected

    row, _, result, dispatch, recover = setup_render(saved, monkeypatch)
    dispatch.side_effect = [EditorialRenderRejected("local configuration"), result]
    assert render.render_project(str(row.id), 1)["status"] == "blocked"
    resumed = control_run(
        row.channel_profile_id, row.project_id, row.id, expected_attempt=1, cancel=False
    )
    assert render.render_project(str(row.id), resumed.attempt)["status"] == "completed"
    assert dispatch.call_count == 2
    recover.assert_not_called()
