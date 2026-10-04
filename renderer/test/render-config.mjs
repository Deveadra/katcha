import assert from 'node:assert/strict';
import {
  resolveRenderSettings,
  validateLambdaSettings,
} from '../src/render-config.mjs';

const defaults = resolveRenderSettings({});
assert.deepEqual(defaults, {
  backend: 'local',
  concurrency: 1,
  externalCompute: {
    enabled: false,
    coordinatorUrl: null,
    token: null,
    maxRenderCostUsd: 0,
    reservationTtlSeconds: 0,
  },
  timeoutInMilliseconds: 120000,
  lambda: {
    expectedAccountId: null,
    region: 'us-east-1',
    functionName: null,
    serveUrl: null,
    pollIntervalMs: 2000,
    maxWaitMs: 1500000,
    maxRetries: 2,
    framesPerLambda: 20,
    concurrencyPerLambda: 1,
    stagingBucket: null,
    stagingPrefix: 'katcha-render-staging',
    stagingUrlExpiresSeconds: 3600,
  },
});
assert.doesNotThrow(() => validateLambdaSettings(defaults));

const budgetEnv = {
  KATCHA_EXTERNAL_COMPUTE_ENABLED: 'true',
  KATCHA_EXTERNAL_COMPUTE_COORDINATOR_URL: 'https://recovery.example.test',
  KATCHA_EXTERNAL_COMPUTE_TOKEN: 'compute-token-with-more-than-32-characters',
  KATCHA_REMOTION_LAMBDA_MAX_RENDER_COST_USD: '0.50',
  KATCHA_REMOTION_LAMBDA_BUDGET_TTL_SECONDS: '7200',
};

const lambdaEnv = {
  ...budgetEnv,
  KATCHA_RENDER_BACKEND: 'lambda',
  KATCHA_AWS_EXPECTED_ACCOUNT_ID: '123456789012',
  KATCHA_REMOTION_LAMBDA_FUNCTION_NAME: 'remotion-render-test',
  KATCHA_REMOTION_LAMBDA_SERVE_URL: 'https://example.com/site',
};

const cloud = resolveRenderSettings({
  ...lambdaEnv,
  KATCHA_RENDER_CONCURRENCY: '3',
  KATCHA_RENDER_TIMEOUT_MS: '180000',
  KATCHA_REMOTION_LAMBDA_REGION: 'us-east-2',
  KATCHA_REMOTION_LAMBDA_FUNCTION_NAME: 'remotion-render-4-0-529-test',
  KATCHA_REMOTION_LAMBDA_SERVE_URL:
    'https://example.s3.us-east-2.amazonaws.com/sites/katcha/index.html',
  KATCHA_REMOTION_LAMBDA_POLL_INTERVAL_MS: '3000',
  KATCHA_REMOTION_LAMBDA_MAX_WAIT_MS: '900000',
  KATCHA_REMOTION_LAMBDA_MAX_RETRIES: '1',
  KATCHA_REMOTION_LAMBDA_FRAMES_PER_LAMBDA: '30',
  KATCHA_REMOTION_LAMBDA_CONCURRENCY_PER_LAMBDA: '1',
  KATCHA_REMOTION_STAGING_BUCKET: 'katcha-render-staging-123456789012',
  KATCHA_REMOTION_STAGING_PREFIX: 'temporary/render-inputs',
  KATCHA_REMOTION_STAGING_URL_EXPIRES_SECONDS: '3600',
});
assert.equal(cloud.backend, 'lambda');
assert.equal(cloud.concurrency, 3);
assert.equal(cloud.timeoutInMilliseconds, 180000);
assert.equal(cloud.lambda.expectedAccountId, '123456789012');
assert.equal(cloud.lambda.region, 'us-east-2');
assert.equal(cloud.lambda.framesPerLambda, 30);
assert.equal(cloud.lambda.stagingBucket, 'katcha-render-staging-123456789012');
assert.equal(cloud.lambda.stagingPrefix, 'temporary/render-inputs');
assert.equal(cloud.externalCompute.maxRenderCostUsd, 0.5);
assert.equal(cloud.externalCompute.reservationTtlSeconds, 7200);
assert.doesNotThrow(() => validateLambdaSettings(cloud));

assert.throws(
  () => validateLambdaSettings(resolveRenderSettings({KATCHA_RENDER_BACKEND: 'lambda'})),
  /EXPECTED_ACCOUNT_ID is required/,
);
assert.throws(
  () => resolveRenderSettings({KATCHA_RENDER_BACKEND: 'unknown'}),
  /unsupported render backend/,
);
assert.throws(
  () => validateLambdaSettings(resolveRenderSettings({
    ...lambdaEnv,
    KATCHA_EXTERNAL_COMPUTE_ENABLED: 'false',
  })),
  /EXTERNAL_COMPUTE_ENABLED must be true/,
);
assert.throws(
  () => validateLambdaSettings(resolveRenderSettings({
    ...lambdaEnv,
    KATCHA_EXTERNAL_COMPUTE_COORDINATOR_URL: 'http://localhost:8788',
  })),
  /public HTTPS URL/,
);
assert.throws(
  () => validateLambdaSettings(resolveRenderSettings({
    ...lambdaEnv,
    KATCHA_EXTERNAL_COMPUTE_TOKEN: 'short',
  })),
  /at least 32 characters/,
);
assert.throws(
  () => validateLambdaSettings(resolveRenderSettings({
    ...lambdaEnv,
    KATCHA_REMOTION_LAMBDA_MAX_RENDER_COST_USD: '0',
  })),
  /MAX_RENDER_COST_USD must be positive/,
);
assert.throws(
  () => validateLambdaSettings(resolveRenderSettings({
    ...lambdaEnv,
    KATCHA_REMOTION_LAMBDA_BUDGET_TTL_SECONDS: '60',
  })),
  /BUDGET_TTL_SECONDS/,
);
assert.throws(
  () => validateLambdaSettings(resolveRenderSettings({
    ...lambdaEnv,
    KATCHA_REMOTION_LAMBDA_MAX_WAIT_MS: '1500000',
    KATCHA_REMOTION_STAGING_BUCKET: 'Invalid_Bucket',
  })),
  /not a valid S3 bucket name/,
);
assert.throws(
  () => validateLambdaSettings(resolveRenderSettings({
    ...lambdaEnv,
    KATCHA_REMOTION_LAMBDA_MAX_WAIT_MS: '1500000',
    KATCHA_REMOTION_STAGING_BUCKET: 'katcha-staging-example',
    KATCHA_REMOTION_STAGING_URL_EXPIRES_SECONDS: '1700',
  })),
  /must exceed the Lambda wait limit/,
);

const belowMinimumFrames = resolveRenderSettings({
  KATCHA_REMOTION_LAMBDA_FRAMES_PER_LAMBDA: '3',
});
assert.equal(
  belowMinimumFrames.lambda.framesPerLambda,
  20,
  'framesPerLambda below Remotion minimum must fall back safely',
);

assert.throws(
  () => validateLambdaSettings(resolveRenderSettings({
    ...lambdaEnv,
    KATCHA_REMOTION_STAGING_BUCKET: 'katcha-staging-example',
    KATCHA_REMOTION_STAGING_PREFIX: '/unsafe-prefix/',
  })),
  /normalized S3 prefix/,
);

assert.throws(
  () => validateLambdaSettings(resolveRenderSettings({
    ...lambdaEnv,
    KATCHA_REMOTION_LAMBDA_REGION: 'moon-1',
  })),
  /not supported by Remotion Lambda/,
);

console.log('PASS: renderer runtime settings are conservative, explicit, and budget-safe.');
