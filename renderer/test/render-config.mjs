import assert from 'node:assert/strict';
import {
  resolveRenderSettings,
  validateLambdaSettings,
} from '../src/render-config.mjs';

const defaults = resolveRenderSettings({});
assert.deepEqual(defaults, {
  backend: 'local',
  concurrency: 1,
  timeoutInMilliseconds: 120000,
  lambda: {
    region: 'us-east-1',
    functionName: null,
    serveUrl: null,
    pollIntervalMs: 2000,
    maxWaitMs: 1800000,
    maxRetries: 2,
    framesPerLambda: 20,
    concurrencyPerLambda: 1,
  },
});
assert.doesNotThrow(() => validateLambdaSettings(defaults));

const cloud = resolveRenderSettings({
  KATCHA_RENDER_BACKEND: 'lambda',
  KATCHA_RENDER_CONCURRENCY: '3',
  KATCHA_RENDER_TIMEOUT_MS: '180000',
  KATCHA_REMOTION_LAMBDA_REGION: 'us-east-2',
  KATCHA_REMOTION_LAMBDA_FUNCTION_NAME: 'remotion-render-4-0-529-test',
  KATCHA_REMOTION_LAMBDA_SERVE_URL: 'https://example.s3.us-east-2.amazonaws.com/sites/katcha/index.html',
  KATCHA_REMOTION_LAMBDA_POLL_INTERVAL_MS: '3000',
  KATCHA_REMOTION_LAMBDA_MAX_WAIT_MS: '900000',
  KATCHA_REMOTION_LAMBDA_MAX_RETRIES: '1',
  KATCHA_REMOTION_LAMBDA_FRAMES_PER_LAMBDA: '30',
  KATCHA_REMOTION_LAMBDA_CONCURRENCY_PER_LAMBDA: '1',
});
assert.equal(cloud.backend, 'lambda');
assert.equal(cloud.concurrency, 3);
assert.equal(cloud.timeoutInMilliseconds, 180000);
assert.equal(cloud.lambda.region, 'us-east-2');
assert.equal(cloud.lambda.framesPerLambda, 30);
assert.doesNotThrow(() => validateLambdaSettings(cloud));

assert.throws(
  () => validateLambdaSettings(resolveRenderSettings({KATCHA_RENDER_BACKEND: 'lambda'})),
  /FUNCTION_NAME is required/,
);
assert.throws(
  () => resolveRenderSettings({KATCHA_RENDER_BACKEND: 'unknown'}),
  /unsupported render backend/,
);

console.log('PASS: renderer runtime settings are conservative, explicit, and cloud-safe.');
