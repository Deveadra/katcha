import {getSites, speculateFunctionName} from '@remotion/lambda/client';
import {pathToFileURL} from 'node:url';

export const DEFAULT_FUNCTION_MEMORY_MB = 4096;
export const DEFAULT_FUNCTION_DISK_MB = 4096;
export const DEFAULT_FUNCTION_TIMEOUT_SECONDS = 900;
export const DEFAULT_SITE_NAME = 'katcha-production';

export const speculateKatchaFunctionName = ({
  memorySizeInMb = DEFAULT_FUNCTION_MEMORY_MB,
  diskSizeInMb = DEFAULT_FUNCTION_DISK_MB,
  timeoutInSeconds = DEFAULT_FUNCTION_TIMEOUT_SECONDS,
} = {}) =>
  speculateFunctionName({
    memorySizeInMb,
    diskSizeInMb,
    timeoutInSeconds,
  });

export const findCompatibleSite = async ({region, siteName = DEFAULT_SITE_NAME}) => {
  if (!region) {
    throw new Error('region is required');
  }
  if (!siteName) {
    throw new Error('siteName is required');
  }

  const {sites} = await getSites({
    region,
    compatibleOnly: true,
  });

  return sites.find((site) => site.id === siteName) || null;
};

const runCli = async () => {
  const [command, ...args] = process.argv.slice(2);

  if (command === 'function-name') {
    const [memoryRaw, diskRaw, timeoutRaw] = args;
    const memorySizeInMb = Number(memoryRaw || DEFAULT_FUNCTION_MEMORY_MB);
    const diskSizeInMb = Number(diskRaw || DEFAULT_FUNCTION_DISK_MB);
    const timeoutInSeconds = Number(timeoutRaw || DEFAULT_FUNCTION_TIMEOUT_SECONDS);

    if (
      !Number.isInteger(memorySizeInMb)
      || !Number.isInteger(diskSizeInMb)
      || !Number.isInteger(timeoutInSeconds)
    ) {
      throw new Error('function-name arguments must be integers');
    }

    process.stdout.write(
      speculateKatchaFunctionName({
        memorySizeInMb,
        diskSizeInMb,
        timeoutInSeconds,
      }) + '\n',
    );
    return;
  }

  if (command === 'site') {
    const [region, siteName = DEFAULT_SITE_NAME] = args;
    const site = await findCompatibleSite({region, siteName});
    if (!site) {
      process.stderr.write(
        `No compatible Remotion site named "${siteName}" found in ${region}.\n`,
      );
      process.exitCode = 4;
      return;
    }
    process.stdout.write(JSON.stringify({
      id: site.id,
      bucketName: site.bucketName,
      serveUrl: site.serveUrl,
      version: site.version,
      lastModified: site.lastModified,
      sizeInBytes: site.sizeInBytes,
    }) + '\n');
    return;
  }

  throw new Error(
    'Usage: node src/lambda-deployment-state.mjs function-name [memoryMb diskMb timeoutSec] '
    + '| site <region> [siteName]',
  );
};

if (import.meta.url === pathToFileURL(process.argv[1] || '').href) {
  runCli().catch((error) => {
    console.error(error?.stack || error);
    process.exitCode = 1;
  });
}
