import assert from 'node:assert/strict';

import {
  DEFAULT_FUNCTION_DISK_MB,
  DEFAULT_FUNCTION_MEMORY_MB,
  DEFAULT_FUNCTION_TIMEOUT_SECONDS,
  DEFAULT_SITE_NAME,
  speculateKatchaFunctionName,
} from '../src/lambda-deployment-state.mjs';

assert.equal(DEFAULT_FUNCTION_MEMORY_MB, 4096);
assert.equal(DEFAULT_FUNCTION_DISK_MB, 4096);
assert.equal(DEFAULT_FUNCTION_TIMEOUT_SECONDS, 900);
assert.equal(DEFAULT_SITE_NAME, 'katcha-production');

const name = speculateKatchaFunctionName();
assert.equal(
  name,
  'remotion-render-4-0-529-mem4096mb-disk4096mb-900sec',
);

console.log(`PASS: deterministic Remotion function name ${name}`);
