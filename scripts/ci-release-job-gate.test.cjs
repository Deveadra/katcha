'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const {
  REQUIRED_PUSH_JOBS, assessCiJobs, waitForCompletedCiJobs,
} = require('./ci-release-job-gate.cjs');
const SHA = 'a'.repeat(40);
const good = () => REQUIRED_PUSH_JOBS.map((name, i) => ({
  name, id: i + 1, status: 'completed', conclusion: 'success',
  head_sha: SHA, run_attempt: 1,
}));
const check = (jobs) => assessCiJobs(jobs, SHA, 1);

test('requires every successful job and correct revision/attempt', () => {
  assert.equal(check(good()).ok, true);
  assert.equal(check([]).ok, false);
  assert.equal(check(good().slice(1)).ok, false);
  assert.equal(check([...good(), {...good()[0], id: 999}]).ok, false);
  assert.equal(check([...good(), {id: 99, name: 'new check', status: 'completed', conclusion: 'failure'}]).ok, false);
  for (const conclusion of [null, 'skipped', 'cancelled', 'failure', 'timed_out']) {
    const jobs = good();
    jobs[6] = {...jobs[6], status: 'completed', conclusion};
    assert.equal(check(jobs).ok, false, String(conclusion));
  }
  for (const status of ['queued', 'in_progress', 'pending', 'waiting']) {
    const jobs = good();
    jobs[6] = {...jobs[6], status, conclusion: null};
    assert.equal(check(jobs).ok, false, status);
  }
  const otherSha = good();
  otherSha[6].head_sha = 'b'.repeat(40);
  assert.equal(check(otherSha).ok, false);
  const otherAttempt = good();
  otherAttempt[6].run_attempt = 2;
  assert.equal(check(otherAttempt).ok, false);
});

test('bounded polling permits eventual consistency but never permits unresolved jobs', async () => {
  const pending = good();
  pending[6] = {...pending[6], status: 'queued', conclusion: null};
  let reads = 0;
  const api = (responses) => ({
    rest: { actions: { listJobsForWorkflowRun: () => {} } },
    paginate: async () => responses[Math.min(reads++, responses.length - 1)],
  });
  const args = {owner: 'Deveadra', repo: 'katcha', runId: 123,
    headSha: SHA, runAttempt: 1, attempts: 3, sleep: async () => {}};
  assert.equal((await waitForCompletedCiJobs({...args, github: api([pending, good()])})).length, good().length);
  assert.equal(reads, 2);
  reads = 0;
  await assert.rejects(
    waitForCompletedCiJobs({...args, github: api([pending])}), /Rejected image publication/
  );
  assert.equal(reads, 3);
  reads = 0;
  const cancelled = good();
  cancelled[6] = {...cancelled[6], status: 'completed', conclusion: 'cancelled'};
  await assert.rejects(
    waitForCompletedCiJobs({...args, github: api([cancelled])}), /Rejected image publication/
  );
  await assert.rejects(
    waitForCompletedCiJobs({...args, github: {rest: {actions: {}}, paginate: async () => { throw Error('503'); }}}),
    /Rejected image publication/
  );
  await assert.rejects(
    waitForCompletedCiJobs({...args, runAttempt: 0, github: api([good()])}), /invalid CI run identity/
  );
});
