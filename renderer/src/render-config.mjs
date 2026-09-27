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

const backend = (raw) => {
  const value = String(raw ?? 'local').trim().toLowerCase();
  if (!['local', 'lambda'].includes(value)) {
    throw new Error(`unsupported render backend: ${value}`);
  }
  return value;
};

export const resolveRenderSettings = (env = process.env) => ({
  backend: backend(env.KATCHA_RENDER_BACKEND),
  concurrency: positiveInteger(env.KATCHA_RENDER_CONCURRENCY, 1),
  timeoutInMilliseconds: positiveInteger(
    env.KATCHA_RENDER_TIMEOUT_MS,
    120000,
    30000,
  ),
  lambda: {
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
      1,
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
  if (!settings.lambda.functionName) {
    throw new Error('KATCHA_REMOTION_LAMBDA_FUNCTION_NAME is required for lambda rendering');
  }
  if (!settings.lambda.serveUrl) {
    throw new Error('KATCHA_REMOTION_LAMBDA_SERVE_URL is required for lambda rendering');
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
