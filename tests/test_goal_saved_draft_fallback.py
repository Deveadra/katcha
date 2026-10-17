"""A provider outage must not hide a draft that actually finished rendering."""

import uuid

import pytest
from test_command_goals import receipt
from test_command_goals import saved as saved_fixture

from katcha import db
from katcha.goal_models import CommandGoal
from katcha.production_models import Production, ProductionAsset
from katcha.services import goal_runner
from katcha.services.goal_receipts import get_goal

saved = saved_fixture


@pytest.mark.parametrize('tool', ['make_short', 'recover_render'])
async def test_planner_outage_keeps_verified_draft_link_and_does_not_claim_completion(
    saved, monkeypatch, tool,
):
    goal = receipt(saved[0], prompt='Make a draft and also do another task')
    production_id = uuid.uuid4()
    with db.session_scope() as session:
        session.add(Production(
            id=production_id, clip_id=uuid.uuid4(), channel_profile_id=saved[0],
            workflow_id=str(uuid.uuid4()), persona_key='host', persona_version='1',
            prompt_version='1', status='review', stage='render_verified',
            render_manifest={'output_key': 'verified/draft.mp4'},
        ))
        session.add(ProductionAsset(
            production_id=production_id, kind='render', storage_key='verified/draft.mp4',
        ))
        current = session.get(CommandGoal, goal.id)
        current.observations = [{
            'step': 0, 'tool': tool, 'error': None,
            'result': {
                ('child_production_id' if tool == 'recover_render' else 'production_id'):
                    str(production_id),
                'workflow_state': 'COMPLETED',
            },
        }]
        current.step_count = 1

    def unavailable(**kwargs):
        raise ValueError('Live goal planning is unavailable. codex: rate limit')

    monkeypatch.setattr(goal_runner, 'decide_goal', unavailable)
    assert await goal_runner.advance_goal(goal.id) == 'blocked'
    current = get_goal(goal.id)
    assert f'/channels?channel={saved[0]}#content' in current.summary
    assert 'ready to watch' in current.result['answer']
    assert 'could not finish checking the rest' in current.summary
    assert current.error == 'Live goal planning is unavailable. codex: rate limit'
    assert current.step_count == 1
    assert current.result['actions'] == []
    assert await goal_runner.advance_goal(goal.id) == 'blocked'


@pytest.mark.parametrize('case', [
    'foreign_channel', 'unfinished', 'failed_observation', 'missing_asset',
    'superseded_asset', 'read_only_observation', 'invalid_id',
])
def test_fallback_does_not_invent_ready_or_cross_channel_outputs(saved, case):
    goal = receipt(saved[0])
    production_id = uuid.uuid4()
    with db.session_scope() as session:
        session.add(Production(
            id=production_id, clip_id=uuid.uuid4(),
            channel_profile_id=uuid.uuid4() if case == 'foreign_channel' else saved[0],
            workflow_id=str(uuid.uuid4()), persona_key='host', persona_version='1',
            prompt_version='1', status='review',
            stage='render' if case == 'unfinished' else 'render_verified',
            render_manifest={'output_key': 'verified/draft.mp4'},
        ))
        if case != 'missing_asset':
            session.add(ProductionAsset(
                production_id=production_id, kind='render',
                storage_key='old.mp4' if case == 'superseded_asset' else 'verified/draft.mp4',
            ))
    goal.observations = [{
        'tool': 'production' if case == 'read_only_observation' else 'make_short',
        'error': 'failed' if case == 'failed_observation' else None,
        'result': {'production_id': 'bad' if case == 'invalid_id' else str(production_id)},
    }]
    assert goal_runner._saved_draft_summary(goal) == ''
