"""Real process exclusion and workflow waiting; no provider/network dependencies."""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from temporalio import activity
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

from katcha.acquisition.worker import Journal, LeaseLost, Worker
from katcha.config import Settings
from katcha.local_resources import ResourceBusy, resource_slot
from katcha.orchestration import resource_queue as queue
from katcha.orchestration import resource_worker


def test_lock_excludes_other_process_and_releases_after_crash(tmp_path):
    lock = tmp_path / 'host.lock'
    code = '''import sys, time
from pathlib import Path
from katcha.local_resources import resource_slot
with resource_slot(Path(sys.argv[1])):
    print('locked', flush=True)
    time.sleep(30)
'''
    process = subprocess.Popen(
        [sys.executable, '-c', code, str(lock)], stdout=subprocess.PIPE, text=True,
    )
    try:
        assert process.stdout.readline().strip() == 'locked'
        with pytest.raises(ResourceBusy), resource_slot(lock, ticket='waiting'):
            pytest.fail('another process owns the resource')
    finally:
        process.terminate()
        process.wait(timeout=10)
    with resource_slot(lock, ticket='waiting'):
        pass


def test_waiters_preserve_order_and_abandoned_waiters_expire(tmp_path):
    lock = tmp_path / 'host.lock'
    with resource_slot(lock):
        for ticket in ['first', 'second']:
            with pytest.raises(ResourceBusy), resource_slot(lock, ticket=ticket):
                pass
    with pytest.raises(ResourceBusy), resource_slot(lock, ticket='second'):
        pass
    with resource_slot(lock, ticket='first'):
        pass
    with resource_slot(lock, ticket='second'):
        pass
    with resource_slot(lock), pytest.raises(ResourceBusy), resource_slot(lock, ticket='abandoned'):
        pass
    for path in tmp_path.glob('*.waiter'):
        os.utime(path, (1, 1))
    with resource_slot(lock, ticket='new'):
        pass


def test_missing_shared_directory_fails_closed(tmp_path):
    with pytest.raises(FileNotFoundError), resource_slot(tmp_path / 'missing' / 'host.lock'):
        pytest.fail('must not silently create a private lock directory')


def test_local_acquirer_never_claims_until_resource_is_available(tmp_path):
    worker = Worker.__new__(Worker)
    worker.resource_lock_path = tmp_path / 'host.lock'
    calls = []
    worker._run_job = lambda journal: calls.append(journal.state['job_id'])
    journal = Journal(tmp_path / 'state', str(uuid.uuid4()), str(uuid.uuid4()))
    with resource_slot(worker.resource_lock_path):
        worker.run_job(journal)
    assert calls == []
    assert journal.state['phase'] == 'new'
    worker.run_job(journal)
    assert calls == [journal.state['job_id']]


def test_queued_variant_preserves_payload_types_and_busy_has_no_side_effect(tmp_path, monkeypatch):
    calls = []

    @activity.defn
    def operation(identity: str) -> dict[str, str]:
        calls.append(identity)
        return {'identity': identity}

    lock = tmp_path / 'host.lock'
    monkeypatch.setattr(resource_worker, 'get_settings', lambda: Settings(
        _env_file=None, local_degraded_mode=True, local_resource_lock_path=lock,
    ))
    monkeypatch.setattr(activity, 'info', lambda: SimpleNamespace(workflow_run_id='run'))
    wrapped = resource_worker.queued_activity(operation)
    definition = activity._Definition.must_from_callable(wrapped)
    assert definition.arg_types == [str]
    assert definition.ret_type == dict[str, str]
    with resource_slot(lock):
        with pytest.raises(ApplicationError) as error:
            wrapped('source')
        assert error.value.type == queue.RESOURCE_BUSY
        assert error.value.non_retryable
    assert calls == []
    assert wrapped('source') == {'identity': 'source'}
    assert calls == ['source']


def activity_failure(kind):
    error = ActivityError(
        'activity failed', scheduled_event_id=1, started_event_id=2,
        identity='worker', activity_type='ingest_source', activity_id='task', retry_state=None,
    )
    error.__cause__ = ApplicationError('test', type=kind, non_retryable=True)
    return error


async def test_busy_waits_durably_without_consuming_operation_retry_policy(monkeypatch):
    calls, sleeps = [], []
    policy = RetryPolicy(maximum_attempts=1)

    async def execute(name, *args, **kwargs):
        calls.append((name, args, kwargs))
        if len(calls) <= 5:
            raise activity_failure(queue.RESOURCE_BUSY)
        return {'done': True}

    async def sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(queue.workflow, 'patched', lambda _: True)
    monkeypatch.setattr(queue.workflow, 'execute_activity', execute)
    monkeypatch.setattr(queue.workflow, 'sleep', sleep)
    result = await queue.execute_resource_activity(
        'ingest_source', 'source', retry_policy=policy,
        start_to_close_timeout=timedelta(minutes=20), task_queue='ingest',
    )
    assert result == {'done': True}
    assert len(sleeps) == 5
    assert all(call[0] == 'resource_queued_ingest_source' for call in calls)
    assert all(call[2]['retry_policy'] is policy for call in calls)
    assert all(call[2]['start_to_close_timeout'] == timedelta(minutes=20) for call in calls)


async def test_real_failures_and_cancellation_are_never_queued_again(monkeypatch):
    monkeypatch.setattr(queue.workflow, 'patched', lambda _: True)
    for error in [activity_failure('SourceDenied'), asyncio.CancelledError()]:
        async def execute(*args, failure=error, **kwargs):
            raise failure
        monkeypatch.setattr(queue.workflow, 'execute_activity', execute)
        with pytest.raises(type(error)):
            await queue.execute_resource_activity('ingest_source', 'source')


async def test_old_workflow_history_keeps_original_activity_name(monkeypatch):
    calls = []

    async def execute(name, *args, **kwargs):
        calls.append(name)
        return 'original'

    monkeypatch.setattr(queue.workflow, 'patched', lambda _: False)
    monkeypatch.setattr(queue.workflow, 'execute_activity', execute)
    assert await queue.execute_resource_activity('ingest_source', 'source') == 'original'
    assert calls == ['ingest_source']


def test_interrupted_ticket_write_does_not_poison_queue(tmp_path):
    lock = tmp_path / 'host.lock'
    for value in ['', 'partial', '-1']:
        broken = tmp_path / 'host.lock.interrupted.waiter'
        broken.write_text(value)
        with resource_slot(lock, ticket='healthy'):
            assert not broken.exists()


def test_unrecoverable_journal_does_not_starve_later_jobs(tmp_path):
    worker = Worker.__new__(Worker)
    worker.state_dir = tmp_path / 'state'
    worker.resource_lock_path = tmp_path / 'host.lock'
    channel = str(uuid.uuid4())
    worker.channels = [channel]
    jobs = sorted([str(uuid.uuid4()), str(uuid.uuid4())])
    for job in jobs:
        Journal(worker.state_dir, channel, job).update(phase='acquired')
    calls = []

    def run(journal):
        calls.append(journal.state['job_id'])
        if journal.state['job_id'] == jobs[0]:
            raise LeaseLost('requires reconciliation')

    worker.run_job = run
    worker.once()
    assert calls == jobs
    assert Journal(worker.state_dir, channel, jobs[0]).state['phase'] == 'acquired'


async def test_media_workflows_prepare_in_temporal_sandbox():
    import importlib

    from temporalio import workflow
    from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

    names = [
        'workflows', 'acquisition_workflows', 'analysis_workflows',
        'brand_preview_workflows', 'editorial_workflows', 'longform_workflows',
        'production_workflows', 'publishing_workflows', 'scene_index_workflows',
        'short_episode_workflows',
    ]
    checked = set()
    for name in names:
        module = importlib.import_module(f'katcha.orchestration.{name}')
        for candidate in vars(module).values():
            if not isinstance(candidate, type):
                continue
            definition = workflow._Definition.from_class(candidate)
            if definition is None or definition.name in checked:
                continue
            SandboxedWorkflowRunner().prepare_workflow(definition)
            checked.add(definition.name)
    assert 'RemoteAcquisitionWorkflow' in checked
    assert 'ClipIngestWorkflow' in checked
