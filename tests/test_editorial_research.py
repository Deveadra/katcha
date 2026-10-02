"""Synthetic providers exercise real persistence and orchestration, not live acceptance."""

import json
import socket
from unittest.mock import Mock

import pytest
from test_editorial_projects import draft
from test_editorial_projects import saved as _saved
from test_editorial_runs import new_project, new_run

from katcha import db
from katcha.editorial import provider, research, retrieval
from katcha.editorial.project_schemas import EditorialDraft
from katcha.editorial.research_schemas import ResearchPlan
from katcha.editorial.run_schemas import StartEditorialRun
from katcha.editorial_models import EditorialProject, EditorialRevision
from katcha.models import UsageEvent
from katcha.orchestration import editorial_activities as activities
from katcha.orchestration import editorial_workflows as workflows
from katcha.services.editorial_projects import EditorialConflict
from katcha.services.editorial_runs import (
    EditorialStopped,
    checkpoint,
    control_run,
    finish_script,
    get_run,
    start_run,
)

saved = _saved


def question(text="What does the symbol mean?"):
    return {"question": text, "specialty": "official", "reason": "Explain the observed symbol"}


def output(value):
    return provider.ProviderOutput(json.dumps(value), "codex", "synthetic", 10, 20, [])


def test_completed_provider_receipt_reuses_without_provider_connection(saved, monkeypatch):
    row = new_run(saved)
    select = Mock(return_value="codex")
    invoke = Mock(return_value=output({"questions": [question()]}))
    monkeypatch.setattr(provider, "select_provider", select)
    monkeypatch.setattr(provider, "_invoke", invoke)
    args = (str(row.id), 1, "plan", "Evidence", ResearchPlan)
    value, receipt = provider.structured_call(*args)
    select.side_effect = AssertionError("Cached work must not need a connection")
    assert provider.structured_call(*args) == (value, receipt)
    assert invoke.call_count == 1
    with db.session_scope() as session:
        assert session.query(UsageEvent).count() == 1
    with pytest.raises(provider.EditorialBlocked, match="inputs changed"):
        provider.structured_call(str(row.id), 1, "plan", "Changed evidence", ResearchPlan)


def test_ambiguous_request_never_repeated_after_resume(saved, monkeypatch):
    row = new_run(saved)
    monkeypatch.setattr(provider, "select_provider", lambda **kw: "codex")
    invoke = Mock(side_effect=TimeoutError("Response lost"))
    monkeypatch.setattr(provider, "_invoke", invoke)
    with pytest.raises(TimeoutError):
        provider.structured_call(str(row.id), 1, "plan", "Evidence", ResearchPlan)
    checkpoint(str(row.id), 1, status="failed")
    control_run(row.channel_profile_id, row.project_id, row.id, expected_attempt=1, cancel=False)
    with pytest.raises(provider.EditorialBlocked, match="uncertain"):
        provider.structured_call(str(row.id), 2, "plan", "Evidence", ResearchPlan)
    assert invoke.call_count == 1


def test_provider_call_budget_blocks_before_external_side_effect(saved, monkeypatch):
    channel, project = new_project(saved)
    row = start_run(
        channel,
        project,
        StartEditorialRun(
            expected_revision=0,
            idempotency_key="budget",
            max_model_calls=1,
        ),
        actor="test",
    )
    monkeypatch.setattr(provider, "select_provider", lambda **kw: "codex")
    invoke = Mock(return_value=output({"questions": [question()]}))
    monkeypatch.setattr(provider, "_invoke", invoke)
    provider.structured_call(str(row.id), 1, "one", "Evidence", ResearchPlan)
    with pytest.raises(provider.EditorialBlocked, match="budget"):
        provider.structured_call(str(row.id), 1, "two", "Evidence", ResearchPlan)
    assert invoke.call_count == 1


def test_cancellation_during_provider_call_prevents_promotion(saved, monkeypatch):
    row = new_run(saved)
    monkeypatch.setattr(provider, "select_provider", lambda **kw: "codex")

    def invoke(*args, **kwargs):
        control_run(row.channel_profile_id, row.project_id, row.id, expected_attempt=1, cancel=True)
        return output({"questions": [question()]})

    monkeypatch.setattr(provider, "_invoke", invoke)
    with pytest.raises(EditorialStopped):
        provider.structured_call(str(row.id), 1, "plan", "Evidence", ResearchPlan)
    result = get_run(row.channel_profile_id, row.project_id, row.id)
    assert result.status == "cancelled"
    assert result.artifacts["provider_calls"]["plan"]["status"] == "started"


@pytest.mark.parametrize("failure", ["cancelled", "stale", "event_failure"])
def test_script_completion_and_revision_are_atomic(saved, monkeypatch, failure):
    row = new_run(saved)
    if failure == "cancelled":
        control_run(row.channel_profile_id, row.project_id, row.id, expected_attempt=1, cancel=True)
    elif failure == "stale":
        with db.session_scope() as session:
            session.get(EditorialProject, row.project_id).revision = 1
    else:
        monkeypatch.setattr(
            "katcha.services.editorial_runs._event",
            Mock(side_effect=RuntimeError("Injected crash before commit")),
        )
    with pytest.raises((EditorialStopped, EditorialConflict, RuntimeError)):
        finish_script(str(row.id), 1, EditorialDraft.model_validate(draft()))
    with db.session_scope() as session:
        assert session.query(EditorialRevision).count() == 0
        assert session.get(EditorialProject, row.project_id).revision == (failure == "stale")
    assert get_run(row.channel_profile_id, row.project_id, row.id).status != "completed"


def prepared_script_run(saved):
    channel, project = new_project(saved)
    row = start_run(
        channel,
        project,
        StartEditorialRun(
            expected_revision=0,
            idempotency_key="script",
            target="script",
            max_queries=2,
        ),
        actor="test",
    )
    checkpoint(
        str(row.id),
        1,
        artifacts={
            "observations": draft()["observations"],
            "source_snapshots": {"0": {"sha256": "synthetic"}},
            "sources": {"0": {"clip_id": "not-used-for-completed-snapshot"}},
        },
    )
    return row


def research_responses(monkeypatch, *, bad_quote=False, critic_passes=True):
    calls = []
    extracts = []
    fetch = Mock(
        return_value={
            "url": "https://example.com/interview",
            "original_url": "https://example.com/interview",
            "sha256": "a" * 64,
            "retrieved_at": "2026-10-02T12:00:00Z",
            "truncated": False,
            "text": "The emblem is blue. The emblem originated in the first episode. " * 3,
        }
    )
    monkeypatch.setattr(research, "retrieve_document", fetch)

    def invoke(route, prompt, schema, **kwargs):
        calls.append(schema.__name__)
        if schema.__name__ == "ResearchPlan":
            value = {"questions": [question()]}
        elif schema.__name__ == "ResearchLeads":
            result = output(
                {
                    "leads": [
                        {"url": "https://example.com/interview", "title": "Synthetic interview"},
                        {"url": "https://evil.test/invented", "title": "Ungrounded"},
                    ]
                }
            )
            result.grounded_urls = ["https://example.com/interview"]
            return result
        elif schema.__name__ == "ResearchFindings":
            inputs = json.loads(prompt.split("\n", 1)[1])
            document = inputs["documents"][0]
            excerpt = (
                "The emblem is blue."
                if not extracts
                else "The emblem originated in the first episode."
            )
            extracts.append(excerpt)
            value = {
                "claims": [
                    {**draft()["claims"][0], "text": excerpt, "source_ids": [document["id"]]}
                ],
                "excerpts": [
                    {
                        "source_id": document["id"],
                        "category": "interview",
                        "excerpt": "Invented quotation" if bad_quote else excerpt,
                    }
                ],
                "follow_ups": [question("Where did the emblem originate?")],
            }
        elif schema.__name__ == "EvidenceReview":
            dossier = json.loads(prompt.split("\n", 1)[1])
            value = {
                "reviews": [
                    {
                        "claim_id": claim["id"],
                        "verdict": "supported",
                        "rationale": "Supported as a synthetic theory",
                        "contradictions": [],
                    }
                    for claim in dossier["claims"]
                ]
            }
        elif schema.__name__ == "EditorialScript":
            inputs = json.loads(prompt.split("\n", 1)[1])
            dossier = inputs.get("dossier", inputs.get("draft"))
            value = {
                "title": "Symbol analysis",
                "selection_rationale": "Both clues matter",
                "beats": [
                    {**draft()["script"][0], "claim_ids": [c["id"] for c in dossier["claims"]]}
                ],
            }
        elif schema.__name__ == "EditorialCritique":
            value = {"passed": critic_passes, "issues": [] if critic_passes else ["Weak opening"]}
        else:
            raise AssertionError(schema.__name__)
        return output(value)

    monkeypatch.setattr(provider, "select_provider", lambda **kw: "codex")
    monkeypatch.setattr(provider, "_invoke", invoke)
    return calls, fetch


async def test_registered_workflow_researches_preserves_excerpts_and_saves_script(
    saved, monkeypatch
):
    row = prepared_script_run(saved)
    calls, fetch = research_responses(monkeypatch)
    registry = {function.__name__: function for function in activities.EDITORIAL_ACTIVITIES}

    async def execute(name, *positional, args=None, **kwargs):
        return registry[name](*(args if args is not None else positional))

    monkeypatch.setattr(workflows.workflow, "execute_activity", execute)
    result = await workflows.EditorialProjectWorkflow().run(str(row.id), 1)
    assert result["stage"] == "script_ready"
    stored = get_run(row.channel_profile_id, row.project_id, row.id)
    assert stored.status == "completed"
    assert stored.artifacts["requires_editorial_review"] is True
    assert len(stored.artifacts["research"]["done"]) == 2
    assert fetch.call_count == 1
    assert calls.count("ResearchLeads") == 2
    with db.session_scope() as session:
        revision = session.get(EditorialRevision, (row.project_id, 1))
        assert len(revision.draft["sources"]) == 2
        assert len({s["excerpt"] for s in revision.draft["sources"]}) == 2
        assert (
            revision.draft["claims"][0]["source_ids"] != revision.draft["claims"][1]["source_ids"]
        )
        assert len(revision.draft["script"]) == 1


@pytest.mark.parametrize("bad_quote,critic_passes", [(True, True), (False, False)])
def test_bad_evidence_or_exhausted_critique_cannot_promote(
    saved, monkeypatch, bad_quote, critic_passes
):
    row = prepared_script_run(saved)
    calls, _ = research_responses(monkeypatch, bad_quote=bad_quote, critic_passes=critic_passes)
    result = activities.editorial_research_script(str(row.id), 1)
    assert result["status"] == "blocked"
    with db.session_scope() as session:
        assert session.query(EditorialRevision).count() == 0
    if not critic_passes:
        assert calls.count("EditorialCritique") == 3
        stored = get_run(row.channel_profile_id, row.project_id, row.id)
        assert stored.artifacts["critique"]["issues"] == ["Weak opening"]


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1"])
def test_retrieval_blocks_internal_and_mixed_dns(monkeypatch, address):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **kw: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443)),
        ],
    )
    with pytest.raises(ValueError, match="non-public"):
        retrieval.public_addresses("https://example.com/")


def test_redirect_is_revalidated_before_connecting(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, *a, **kw: [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                ("127.0.0.1" if host == "internal.test" else "8.8.8.8", 443),
            ),
        ],
    )
    connection = Mock()
    connection.getresponse.return_value.status = 302
    connection.getresponse.return_value.getheader.return_value = "https://internal.test/secret"
    factory = Mock(return_value=connection)
    monkeypatch.setattr(retrieval, "_PinnedHTTPS", factory)
    with pytest.raises(ValueError, match="non-public"):
        retrieval.retrieve_document("https://example.com/")
    assert factory.call_count == 1
    connection.close.assert_called_once()


def test_quota_rejection_resumes_only_on_new_attempt_and_same_provider(saved, monkeypatch):
    errors = pytest.importorskip("google.genai.errors")
    row = new_run(saved)
    monkeypatch.setattr(provider, "select_provider", lambda **kw: "gemini")
    invoke = Mock(side_effect=errors.ClientError(429, {"error": {"message": "quota"}}))
    monkeypatch.setattr(provider, "_invoke", invoke)
    with pytest.raises(provider.EditorialBlocked, match="quota"):
        provider.structured_call(str(row.id), 1, "plan", "Evidence", ResearchPlan)
    receipt = get_run(row.channel_profile_id, row.project_id, row.id).artifacts["provider_calls"][
        "plan"
    ]
    assert receipt["status"] == "quota_rejected"
    assert receipt["reserved_tokens"] == 0
    checkpoint(str(row.id), 1, status="blocked")
    control_run(row.channel_profile_id, row.project_id, row.id, expected_attempt=1, cancel=False)
    invoke.side_effect = None
    invoke.return_value = output({"questions": [question()]})
    _, receipt = provider.structured_call(str(row.id), 2, "plan", "Evidence", ResearchPlan)
    assert receipt["submissions"] == 2
    assert invoke.call_count == 2


def test_token_reservation_blocks_before_model_call(saved, monkeypatch):
    channel, project = new_project(saved)
    row = start_run(
        channel,
        project,
        StartEditorialRun(
            expected_revision=0,
            idempotency_key="token-budget",
            max_model_tokens=1000,
        ),
        actor="test",
    )
    monkeypatch.setattr(provider, "select_provider", lambda **kw: "codex")
    invoke = Mock()
    monkeypatch.setattr(provider, "_invoke", invoke)
    with pytest.raises(provider.EditorialBlocked, match="token budget"):
        provider.structured_call(str(row.id), 1, "plan", "Evidence", ResearchPlan)
    invoke.assert_not_called()


def test_resume_after_failure_reuses_entire_research_and_script(saved, monkeypatch):
    row = prepared_script_run(saved)
    calls, fetch = research_responses(monkeypatch)
    real_finish = research.finish_script
    monkeypatch.setattr(research, "finish_script", Mock(side_effect=RuntimeError("crash")))
    with pytest.raises(RuntimeError):
        activities.editorial_research_script(str(row.id), 1)
    call_count = len(calls)
    control_run(row.channel_profile_id, row.project_id, row.id, expected_attempt=1, cancel=False)
    monkeypatch.setattr(research, "finish_script", real_finish)
    assert activities.editorial_research_script(str(row.id), 2)["stage"] == "script_ready"
    assert len(calls) == call_count
    assert fetch.call_count == 1
