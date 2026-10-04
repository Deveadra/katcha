"""History uses immutable identities and never crosses project/channel boundaries."""

import uuid
from datetime import UTC, datetime, timedelta

from test_editorial_projects import brief, root
from test_editorial_projects import saved as _saved

from katcha import db
from katcha.editorial.project_schemas import EditorialDraft, SaveEditorialDraft
from katcha.editorial_models import EditorialRun
from katcha.services.editorial_projects import save_draft

saved = _saved


def add_run(channel, project, identity, created):
    with db.session_scope() as session:
        session.add(
            EditorialRun(
                id=identity,
                project_id=project,
                channel_profile_id=channel,
                input_digest="a" * 64,
                input_revision=0,
                options={"target": "analysis"},
                attempt=1,
                status="completed",
                stage="analysis_ready",
                artifacts={"private": "receipt"},
                actor="test",
                created_at=created,
            )
        )


def test_cursor_survives_new_run_and_timestamp_ties(saved):
    client, channel, _ = saved
    project = uuid.UUID(client.post(root(channel), json=brief()).json()["id"])
    endpoint = f"{root(channel)}/{project}/runs"
    stamp = datetime(2026, 10, 4, tzinfo=UTC)
    identities = [uuid.UUID(int=value) for value in range(1, 7)]
    for identity in reversed(identities):
        add_run(channel, project, identity, stamp)
    first = client.get(endpoint, params={"limit": 2}).json()
    assert [row["editorial_run_id"] for row in first] == list(map(str, identities[:2]))
    assert "artifacts" not in first[0]
    add_run(channel, project, uuid.uuid4(), stamp + timedelta(seconds=1))
    second = client.get(endpoint, params={"limit": 2, "before": first[-1]["editorial_run_id"]})
    assert second.status_code == 200
    assert [row["editorial_run_id"] for row in second.json()] == list(map(str, identities[2:4]))
    third = client.get(endpoint, params={"before": identities[3]}).json()
    assert [row["editorial_run_id"] for row in third] == list(map(str, identities[4:]))
    assert client.get(endpoint, params={"before": identities[-1]}).json() == []
    assert client.get(endpoint, params={"before": identities[0], "offset": 1}).status_code == 422
    assert client.get(endpoint, params={"before": "not-a-uuid"}).status_code == 422
    assert client.get(endpoint, params={"before": uuid.uuid4()}).status_code == 404


def test_history_cursor_and_revision_are_project_and_channel_scoped(saved):
    client, channel, other = saved
    project = uuid.UUID(client.post(root(channel), json=brief()).json()["id"])
    body = brief()
    body["idempotency_key"] = "other-project"
    sibling = uuid.UUID(client.post(root(channel), json=body).json()["id"])
    identity = uuid.uuid4()
    add_run(channel, sibling, identity, datetime.now(UTC))
    endpoint = f"{root(channel)}/{project}"
    assert client.get(f"{endpoint}/runs", params={"before": identity}).status_code == 404
    assert client.get(f"{root(other)}/{project}/runs").status_code == 403
    save_draft(
        channel,
        project,
        SaveEditorialDraft(
            expected_revision=0,
            idempotency_key="revision-one",
            draft=EditorialDraft(),
        ),
        actor="test",
    )
    save_draft(
        channel,
        project,
        SaveEditorialDraft(
            expected_revision=1,
            idempotency_key="revision-two",
            draft=EditorialDraft(),
        ),
        actor="test",
    )
    reader = {"Authorization": "Bearer editorial-reader-token-00001"}
    response = client.get(f"{endpoint}/revisions/1", headers=reader)
    assert response.status_code == 200
    assert response.json()["revision"] == 1
    assert client.get(f"{endpoint}/revisions/2", headers=reader).json()["revision"] == 2
    assert client.get(f"{root(channel)}/{sibling}/revisions/1", headers=reader).status_code == 404
    assert client.get(f"{root(other)}/{project}/revisions/1", headers=reader).status_code == 403
    assert client.get(f"{endpoint}/revisions/999", headers=reader).status_code == 404
    assert (
        client.get(
            f"{endpoint}/revisions/1", headers={"Authorization": "Bearer invalid"}
        ).status_code
        == 401
    )
