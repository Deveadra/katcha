import {createHash, randomUUID} from 'node:crypto';
import net from 'node:net';

const blockedHostnames = new Set([
  'localhost',
  '0.0.0.0',
  '::1',
  'minio',
  'renderer',
  'acceptance-media',
  'host.docker.internal',
]);

const isPrivateIpv4 = (hostname) => {
  if (net.isIP(hostname) !== 4) {
    return false;
  }
  const [a, b] = hostname.split('.').map(Number);
  return (
    a === 10
    || a === 127
    || (a === 169 && b === 254)
    || (a === 172 && b >= 16 && b <= 31)
    || (a === 192 && b === 168)
  );
};

const inspectUrl = (raw) => {
  if (!/^https?:\/\//i.test(raw)) {
    return;
  }
  const url = new URL(raw);
  const hostname = url.hostname.toLowerCase();
  if (url.protocol !== 'https:') {
    throw new Error(`Lambda media URL must use HTTPS: ${hostname}`);
  }
  if (blockedHostnames.has(hostname) || isPrivateIpv4(hostname)) {
    throw new Error(`Lambda cannot reach local/private media host: ${hostname}`);
  }
};

const walk = (value) => {
  if (typeof value === 'string') {
    inspectUrl(value);
    return;
  }
  if (Array.isArray(value)) {
    value.forEach(walk);
    return;
  }
  if (value && typeof value === 'object') {
    Object.values(value).forEach(walk);
  }
};

export const assertCloudReachableInputProps = (inputProps) => {
  walk(inputProps);
};

const sleep = (milliseconds) =>
  new Promise((resolve) => setTimeout(resolve, milliseconds));

const errorSummary = (errors) =>
  (errors || [])
    .slice(0, 3)
    .map((item) => item?.message || item?.stack || JSON.stringify(item))
    .join(' | ');


const outputIdentity = (inputProps) => {
  const outputKey = String(inputProps?.output_key || '').trim();
  if (!outputKey) {
    throw new Error(
      'Lambda render budget requires a stable output_key identity',
    );
  }
  return createHash('sha256').update(outputKey).digest('hex');
};

const renderCeilingMicrousd = (renderSettings) => {
  const usd = Number(renderSettings.externalCompute?.maxRenderCostUsd || 0);
  const microusd = Math.ceil(usd * 1_000_000);
  if (!Number.isSafeInteger(microusd) || microusd <= 0) {
    throw new Error(
      'Lambda render budget ceiling must resolve to a positive safe integer',
    );
  }
  return microusd;
};

const budgetRequest = async ({
  renderSettings,
  path,
  payload,
  fetchImpl = globalThis.fetch,
}) => {
  if (typeof fetchImpl !== 'function') {
    throw new Error('external-compute budget transport is unavailable');
  }
  const compute = renderSettings.externalCompute || {};
  const base = String(compute.coordinatorUrl || '').replace(/\/+$/, '');
  const response = await fetchImpl(`${base}${path}`, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${compute.token}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(payload),
    signal: AbortSignal.timeout(15000),
  });
  let data = null;
  try {
    data = await response.json();
  } catch {
    // Preserve the HTTP status in the error below.
  }
  if (!response.ok) {
    const detail = String(data?.error || response.statusText || 'request denied');
    throw new Error(
      `external-compute budget ${path} returned HTTP ${response.status}: ${detail}`,
    );
  }
  if (!data || typeof data !== 'object') {
    throw new Error(`external-compute budget ${path} returned invalid JSON`);
  }
  return data;
};

export const reserveLambdaBudget = async ({
  compositionId,
  inputProps,
  renderSettings,
  fetchImpl = globalThis.fetch,
  randomUuid = randomUUID,
}) => {
  const identity = outputIdentity(inputProps);
  const estimatedCostMicrousd = renderCeilingMicrousd(renderSettings);
  const result = await budgetRequest({
    renderSettings,
    path: '/v1/external-compute/reserve',
    fetchImpl,
    payload: {
      job_key: `render:${identity}:${randomUuid()}`,
      provider: 'aws-lambda',
      operation: 'remotion-video-render',
      retry_group: `render:${identity}`,
      estimated_cost_microusd: estimatedCostMicrousd,
      ttl_seconds: renderSettings.externalCompute.reservationTtlSeconds,
      settle_on_expiry: true,
      metadata: {
        composition: compositionId,
        output_key_sha256: identity,
        duration_seconds: Number(inputProps?.output_duration_seconds || 0),
      },
    },
  });
  const reservation = result.reservation;
  if (
    !reservation
    || reservation.status !== 'reserved'
    || !String(reservation.id || '').trim()
  ) {
    throw new Error(
      'external-compute budget did not return an active Lambda reservation',
    );
  }
  return {
    id: String(reservation.id),
    attempt: Number(reservation.attempt || 0),
    estimatedCostMicrousd: Number(
      reservation.estimated_cost_microusd || estimatedCostMicrousd,
    ),
  };
};

export const settleLambdaBudget = async ({
  reservation,
  renderSettings,
  reason,
  fetchImpl = globalThis.fetch,
}) => budgetRequest({
  renderSettings,
  path: '/v1/external-compute/settle',
  fetchImpl,
  payload: {
    reservation_id: reservation.id,
    actual_cost_microusd: reservation.estimatedCostMicrousd,
    metadata: {
      settlement_reason: String(reason || '').slice(0, 500),
    },
  },
});

const settleLambdaBudgetBestEffort = async (args) => {
  try {
    await settleLambdaBudget(args);
    return true;
  } catch (error) {
    console.error(
      'Lambda budget settlement failed; reservation remains held',
      error,
    );
    return false;
  }
};

export const renderMediaViaLambda = async ({
  compositionId,
  inputProps,
  outputPath,
  renderSettings,
  dependencies = {},
}) => {
  assertCloudReachableInputProps(inputProps);

  const remotionClient = dependencies.renderMediaOnLambda
    ? dependencies
    : await import('@remotion/lambda/client');
  const remotionDownload = dependencies.downloadMedia
    ? dependencies
    : await import('@remotion/lambda');
  const renderMediaOnLambda = remotionClient.renderMediaOnLambda;
  const getRenderProgress = remotionClient.getRenderProgress;
  const downloadMedia = remotionDownload.downloadMedia;
  const fetchImpl = dependencies.fetch || globalThis.fetch;

  const reservation = await reserveLambdaBudget({
    compositionId,
    inputProps,
    renderSettings,
    fetchImpl,
    randomUuid: dependencies.randomUUID || randomUUID,
  });
  const lambda = renderSettings.lambda;

  try {
    const launch = await renderMediaOnLambda({
      region: lambda.region,
      functionName: lambda.functionName,
      serveUrl: lambda.serveUrl,
      composition: compositionId,
      inputProps,
      codec: 'h264',
      imageFormat: 'jpeg',
      privacy: 'private',
      maxRetries: lambda.maxRetries,
      framesPerLambda: lambda.framesPerLambda,
      concurrencyPerLambda: lambda.concurrencyPerLambda,
      timeoutInMilliseconds: renderSettings.timeoutInMilliseconds,
      logLevel: 'info',
      isProduction: true,
    });

    console.log(
      `lambda render launched render_id=${launch.renderId} region=${lambda.region}`,
    );

    const deadline = Date.now() + lambda.maxWaitMs;
    let progress = null;
    let lastProgressReportAt = 0;
    let lastReportedPercent = -1;
    while (Date.now() < deadline) {
      progress = await getRenderProgress({
        renderId: launch.renderId,
        bucketName: launch.bucketName,
        functionName: lambda.functionName,
        region: lambda.region,
        logLevel: 'info',
      });

      const now = Date.now();
      const percent = Math.max(
        0,
        Math.min(100, Math.floor(Number(progress.overallProgress || 0) * 100)),
      );
      if (
        percent >= lastReportedPercent + 10
        || now - lastProgressReportAt >= 30000
        || progress.done
      ) {
        console.log(
          `lambda render progress render_id=${launch.renderId} progress=${percent}% `
          + `frames=${Number(progress.framesRendered || 0)} `
          + `lambdas=${Number(progress.lambdasInvoked || 0)}`,
        );
        lastProgressReportAt = now;
        lastReportedPercent = percent;
      }

      if (progress.fatalErrorEncountered) {
        const detail = errorSummary(progress.errors);
        throw new Error(
          `Remotion Lambda render failed: ${detail || 'fatal error without details'}; `
          + `render_id=${launch.renderId}; cloudwatch=${launch.cloudWatchLogs}`,
        );
      }

      if (progress.done) {
        const downloaded = await downloadMedia({
          region: lambda.region,
          bucketName: launch.bucketName,
          renderId: launch.renderId,
          outPath: outputPath,
          logLevel: 'info',
        });
        const budgetSettled = await settleLambdaBudgetBestEffort({
          reservation,
          renderSettings,
          reason: 'Remotion Lambda render completed',
          fetchImpl,
        });
        return {
          renderId: launch.renderId,
          bucketName: launch.bucketName,
          lambdasInvoked: Number(progress.lambdasInvoked || 0),
          overallProgress: Number(progress.overallProgress || 1),
          outputSizeInBytes: Number(
            progress.outputSizeInBytes || downloaded.sizeInBytes || 0,
          ),
          timeToFinishMs: progress.timeToFinish,
          estimatedBillingDurationInMilliseconds:
            progress.estimatedBillingDurationInMilliseconds,
          cloudWatchLogs: launch.cloudWatchLogs,
          budgetReservationId: reservation.id,
          budgetReservedMicrousd: reservation.estimatedCostMicrousd,
          budgetAttempt: reservation.attempt,
          budgetSettlementPending: !budgetSettled,
        };
      }

      await sleep(lambda.pollIntervalMs);
    }

    throw new Error(
      `Remotion Lambda render exceeded Katcha wait limit of ${lambda.maxWaitMs}ms; `
      + `render_id=${launch.renderId}; cloudwatch=${launch.cloudWatchLogs}`,
    );
  } catch (error) {
    await settleLambdaBudgetBestEffort({
      reservation,
      renderSettings,
      reason: `Remotion Lambda render failed or became uncertain: ${error?.message || error}`,
      fetchImpl,
    });
    throw error;
  }
};

