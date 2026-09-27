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

export const renderMediaViaLambda = async ({
  compositionId,
  inputProps,
  outputPath,
  renderSettings,
}) => {
  assertCloudReachableInputProps(inputProps);

  const {renderMediaOnLambda, getRenderProgress} = await import('@remotion/lambda/client');
  const {downloadMedia} = await import('@remotion/lambda');
  const lambda = renderSettings.lambda;

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

  const deadline = Date.now() + lambda.maxWaitMs;
  let progress = null;
  while (Date.now() < deadline) {
    progress = await getRenderProgress({
      renderId: launch.renderId,
      bucketName: launch.bucketName,
      functionName: lambda.functionName,
      region: lambda.region,
      logLevel: 'info',
    });

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
      };
    }

    await sleep(lambda.pollIntervalMs);
  }

  throw new Error(
    `Remotion Lambda render exceeded Katcha wait limit of ${lambda.maxWaitMs}ms; `
    + `render_id=${launch.renderId}; cloudwatch=${launch.cloudWatchLogs}`,
  );
};
