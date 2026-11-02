'use strict';

// The full push-to-main CI must exercise every required job. A workflow-level
// success is insufficient if its independent job/check records are unresolved.
const REQUIRED_PUSH_JOBS = Object.freeze([
  'Plan affected checks', 'python', 'terraform', 'acceptance-media',
  'temporal-smoke', 'storage-image', 'renderer', 'ranked-render-e2e',
  'explorer', 'Server browser Chromium acceptance', 'PR Gate',
]);

function assessCiJobs(jobs, headSha, runAttempt) {
  const problems = [];
  if (!Array.isArray(jobs) || jobs.length === 0) {
    return { ok: false, problems: ['GitHub returned no CI jobs'] };
  }
  const seen = new Set();
  for (const job of jobs) {
    if (!job || typeof job.name !== 'string' || !Number.isInteger(job.id)) {
      problems.push('GitHub returned a malformed job record');
      continue;
    }
    if (seen.has(job.name)) problems.push('Duplicate CI job: ' + job.name);
    seen.add(job.name);
    if (job.head_sha && job.head_sha !== headSha) {
      problems.push(job.name + ': different commit SHA');
    }
    if (job.run_attempt != null && job.run_attempt !== runAttempt) {
      problems.push(job.name + ': different run attempt');
    }
    if (job.status !== 'completed' || job.conclusion !== 'success') {
      problems.push(job.name + ': ' + job.status + '/' + job.conclusion);
    }
  }
  for (const required of REQUIRED_PUSH_JOBS) {
    if (!seen.has(required)) problems.push('Missing required CI job: ' + required);
  }
  return { ok: problems.length === 0, problems };
}

async function waitForCompletedCiJobs({
  github, owner, repo, runId, headSha, runAttempt,
  attempts = 6, pauseMs = 4000,
  sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
  log = () => {},
}) {
  if (!Number.isInteger(runId) || runId <= 0 ||
      !Number.isInteger(runAttempt) || runAttempt <= 0 ||
      !/^[0-9a-f]{40}$/.test(headSha)) {
    throw new Error('Missing or invalid CI run identity');
  }
  let last = 'No job status obtained';
  for (let attempt = 1; attempt <= attempts; attempt++) {
    try {
      const jobs = await github.paginate(github.rest.actions.listJobsForWorkflowRun, {
        owner, repo, run_id: runId, filter: 'latest', per_page: 100,
      });
      const decision = assessCiJobs(jobs, headSha, runAttempt);
      if (decision.ok) {
        log('Verified ' + jobs.length + ' completed/success CI jobs for run ' + runId +
            ' attempt ' + runAttempt + ' at ' + headSha);
        return jobs;
      }
      last = decision.problems.join('; ');
    } catch (error) {
      last = 'GitHub CI jobs API unavailable: ' + String(error);
    }
    log('CI job status not yet independently verified (' + attempt + '/' +
        attempts + '): ' + last);
    if (attempt < attempts) await sleep(pauseMs);
  }
  throw new Error('Rejected image publication: CI job evidence remained unresolved: ' + last);
}

module.exports = { REQUIRED_PUSH_JOBS, assessCiJobs, waitForCompletedCiJobs };
