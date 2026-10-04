const positiveInteger = (raw, fallback, minimum = 1) => {
  const parsed = Number.parseInt(String(raw ?? ''), 10);
  if (!Number.isFinite(parsed) || parsed < minimum) {
    return fallback;
  }
  return parsed;
};

const optionalString = (raw) => {
  const value = String(raw ?? '').trim();
  return value || null;
};

const enabled = (raw) =>
  String(raw ?? '').trim().toLowerCase() === 'true';

const nonNegativeNumber = (raw, fallback = 0) => {
  const parsed = Number(String(raw ?? ''));
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : fallback;
};

const backend = (raw) => {
  const value = String(raw ?? 'local').trim().toLowerCase();
  if (!['local', 'lambda'].includes(value)) {
    throw new Error(`unsupported render backend: ${value}`);
  }
  return value;
};

const supportedLambdaRegions = new Set([
  'af-south-1',
  'ap-east-1',
  'ap-northeast-1',
  'ap-northeast-2',
  'ap-northeast-3',
  'ap-south-1',
  'ap-southeast-1',
  'ap-southeast-2',
  'ap-southeast-4',
  'ap-southeast-5',
  'ca-central-1',
  'cn-north-1',
  'cn-northwest-1',
  'eu-central-1',
  'eu-central-2',
  'eu-north-1',
  'eu-south-1',
  'eu-west-1',
  'eu-west-2',
  'eu-west-3',
  'sa-east-1',
  'us-east-1',
  'us-east-2',
  'us-west-1',
  'us-west-2',
]);

export const resolveRenderSettings = (env = process.env) => ({
  backend: backend(env.KATCHA_RENDER_BACKEND),
  concurrency: positiveInteger(env.KATCHA_RENDER_CONCURRENCY, 1),
  externalCompute: {
    enabled: enabled(env.KATCHA_EXTERNAL_COMPUTE_ENABLED),
    coordinatorUrl: optionalString(
      env.KATCHA_EXTERNAL_COMPUTE_COORDINATOR_URL,
    ),
    token: optionalString(env.KATCHA_EXTERNAL_COMPUTE_TOKEN),
    maxRenderCostUsd: nonNegativeNumber(
      env.KATCHA_REMOTION_LAMBDA_MAX_RENDER_COST_USD,
      0,
    ),
    reservationTtlSeconds: positiveInteger(
      env.KATCHA_REMOTION_LAMBDA_BUDGET_TTL_SECONDS,
      0,
      300,
    ),
  },
  timeoutInMilliseconds: positiveInteger(
    env.KATCHA_RENDER_TIMEOUT_MS,
    120000,
    30000,
  ),
  lambda: {
    expectedAccountId: optionalString(env.KATCHA_AWS_EXPECTED_ACCOUNT_ID),
    region: optionalString(env.KATCHA_REMOTION_LAMBDA_REGION) || 'us-east-1',
    functionName: optionalString(env.KATCHA_REMOTION_LAMBDA_FUNCTION_NAME),
    serveUrl: optionalString(env.KATCHA_REMOTION_LAMBDA_SERVE_URL),
    pollIntervalMs: positiveInteger(
      env.KATCHA_REMOTION_LAMBDA_POLL_INTERVAL_MS,
      2000,
      500,
    ),
    maxWaitMs: positiveInteger(
      env.KATCHA_REMOTION_LAMBDA_MAX_WAIT_MS,
      1500000,
      60000,
    ),
    maxRetries: positiveInteger(
      env.KATCHA_REMOTION_LAMBDA_MAX_RETRIES,
      2,
      0,
    ),
    framesPerLambda: positiveInteger(
      env.KATCHA_REMOTION_LAMBDA_FRAMES_PER_LAMBDA,
      20,
      4,
    ),
    concurrencyPerLambda: positiveInteger(
      env.KATCHA_REMOTION_LAMBDA_CONCURRENCY_PER_LAMBDA,
      1,
      1,
    ),
    stagingBucket: optionalString(env.KATCHA_REMOTION_STAGING_BUCKET),
    stagingPrefix:
      optionalString(env.KATCHA_REMOTION_STAGING_PREFIX)
      || 'katcha-render-staging',
    stagingUrlExpiresSeconds: positiveInteger(
      env.KATCHA_REMOTION_STAGING_URL_EXPIRES_SECONDS,
      3600,
      900,
    ),
  },
});

export const validateLambdaSettings = (settings) => {
  if (settings.backend !== 'lambda') {
    return;
  }
  if (!/^\d{12}$/.test(String(settings.lambda.expectedAccountId || ''))) {
    throw new Error(
      'KATCHA_AWS_EXPECTED_ACCOUNT_ID is required and must be exactly 12 digits for lambda rendering',
    );
  }
  if (!supportedLambdaRegions.has(settings.lambda.region)) {
    throw new Error(
      `KATCHA_REMOTION_LAMBDA_REGION is not supported by Remotion Lambda: ${settings.lambda.region}`,
    );
  }
  if (!settings.lambda.functionName) {
    throw new Error('KATCHA_REMOTION_LAMBDA_FUNCTION_NAME is required for lambda rendering');
  }
  if (!settings.lambda.serveUrl) {
    throw new Error('KATCHA_REMOTION_LAMBDA_SERVE_URL is required for lambda rendering');
  }
  if (!settings.externalCompute?.enabled) {
    throw new Error(
      'KATCHA_EXTERNAL_COMPUTE_ENABLED must be true for lambda rendering',
    );
  }
  let coordinatorUrl;
  try {
    coordinatorUrl = new URL(
      String(settings.externalCompute.coordinatorUrl || ''),
    );
  } catch {
    throw new Error(
      'KATCHA_EXTERNAL_COMPUTE_COORDINATOR_URL must be an absolute HTTPS URL',
    );
  }
  if (
    coordinatorUrl.protocol !== 'https:'
    || ['localhost', '127.0.0.1', '::1'].includes(
      coordinatorUrl.hostname.toLowerCase(),
    )
  ) {
    throw new Error(
      'KATCHA_EXTERNAL_COMPUTE_COORDINATOR_URL must be a public HTTPS URL',
    );
  }
  if (String(settings.externalCompute.token || '').length < 32) {
    throw new Error(
      'KATCHA_EXTERNAL_COMPUTE_TOKEN must contain at least 32 characters',
    );
  }
  if (!(Number(settings.externalCompute.maxRenderCostUsd) > 0)) {
    throw new Error(
      'KATCHA_REMOTION_LAMBDA_MAX_RENDER_COST_USD must be positive',
    );
  }
  if (
    !Number.isInteger(settings.externalCompute.reservationTtlSeconds)
    || settings.externalCompute.reservationTtlSeconds < 300
    || settings.externalCompute.reservationTtlSeconds > 604800
  ) {
    throw new Error(
      'KATCHA_REMOTION_LAMBDA_BUDGET_TTL_SECONDS must be between 300 and 604800',
    );
  }
  let url;
  try {
    url = new URL(settings.lambda.serveUrl);
  } catch {
    throw new Error('KATCHA_REMOTION_LAMBDA_SERVE_URL must be an absolute URL');
  }
  if (url.protocol !== 'https:') {
    throw new Error('KATCHA_REMOTION_LAMBDA_SERVE_URL must use HTTPS');
  }
  if (settings.lambda.stagingBucket) {
    const bucket = settings.lambda.stagingBucket;
    const prefix = settings.lambda.stagingPrefix;
    if (!prefix || prefix.startsWith('/') || prefix.endsWith('/') || prefix.includes('//')) {
      throw new Error(
        'KATCHA_REMOTION_STAGING_PREFIX must be a non-empty normalized S3 prefix',
      );
    }
    if (
      bucket.length < 3
      || bucket.length > 63
      || !/^[a-z0-9][a-z0-9.-]*[a-z0-9]$/.test(bucket)
      || bucket.includes('..')
    ) {
      throw new Error('KATCHA_REMOTION_STAGING_BUCKET is not a valid S3 bucket name');
    }
    const requiredExpiry = Math.ceil(settings.lambda.maxWaitMs / 1000) + 300;
    if (settings.lambda.stagingUrlExpiresSeconds < requiredExpiry) {
      throw new Error(
        'KATCHA_REMOTION_STAGING_URL_EXPIRES_SECONDS must exceed the Lambda wait '
        + 'limit by at least 300 seconds',
      );
    }
  }
};
