import asyncio
import uuid
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from katcha.api import main
from katcha.production_models import Production
from katcha.services import goal_tools


def test_saved_production_lookup_filters_clip_and_channel_before_pagination(monkeypatch):
    engine = create_engine('sqlite://')
    Production.__table__.create(engine)
    channel, other_channel, clip, other_clip = [uuid.uuid4() for _ in range(4)]
    with Session(engine) as session:
        rows = []
        for channel_id, clip_id, status in [
            (channel, clip, 'failed'), (channel, clip, 'review'),
            (other_channel, clip, 'review'), (channel, other_clip, 'review'),
        ]:
            row = Production(clip_id=clip_id, channel_profile_id=channel_id,
                             workflow_id=str(uuid.uuid4()), persona_key='host',
                             persona_version='1', prompt_version='1', status=status)
            session.add(row)
            rows.append(row)
        session.commit()
        expected = rows[1].id

    @contextmanager
    def session_scope():
        with Session(engine) as session:
            yield session
    monkeypatch.setattr(main, 'session_scope', session_scope)
    rows = main.list_productions(limit=1, offset=0, production_status='review',
                                 channel_profile_id=channel, clip_id=clip)
    assert [row.id for row in rows] == [expected]
    engine.dispose()


def test_goal_dispatch_uses_production_lookup_schema_and_bound_channel(monkeypatch):
    channel, clip = uuid.uuid4(), uuid.uuid4()
    schema = goal_tools.tool_schema('productions')
    assert 'clip_id' in schema['properties']['query']['properties']
    assert not goal_tools.TOOLS['productions'].mutates
    monkeypatch.setattr(goal_tools, 'resolve_goal_authority', lambda goal: ({'*'}, None))
    monkeypatch.setattr(goal_tools, '_known_ids', lambda goal: {str(clip)})
    monkeypatch.setattr(goal_tools, 'require_native_resource_channels', lambda *a: None)
    app = FastAPI()
    received = []

    @app.get('/v1/productions')
    def productions(channel_profile_id: uuid.UUID, clip_id: uuid.UUID):
        received.append((channel_profile_id, clip_id))
        return [{'id': 'saved-production', 'clip_id': str(clip_id), 'status': 'failed'}]

    monkeypatch.setattr(main, 'app', app)
    goal = SimpleNamespace(channel_profile_id=channel, actor='local-development')
    result = asyncio.run(goal_tools.run_native_tool(
        goal, 'productions', {'query': {'clip_id': str(clip)}}, uuid.uuid4()
    ))
    assert received == [(channel, clip)]
    assert result['items'][0]['status'] == 'failed'
    with pytest.raises(ValueError, match='different channel'):
        asyncio.run(goal_tools.run_native_tool(
            goal, 'productions', {'query': {'channel_profile_id': str(uuid.uuid4()),
                                           'clip_id': str(clip)}}, uuid.uuid4()
        ))
    with pytest.raises(ValueError, match='was not observed'):
        asyncio.run(goal_tools.run_native_tool(
            goal, 'productions', {'query': {'clip_id': str(uuid.uuid4())}}, uuid.uuid4()
        ))
