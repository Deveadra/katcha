import {S3Client} from '@aws-sdk/client-s3';

export const createRenderS3Client = (options) => new S3Client({
  ...options,
  // R2/HTTP response streams may yield small chunks anywhere in the body.
  // Buffer before AWS checksum framing, which requires nonfinal chunks >=8KiB.
  // This keeps memory bounded and retains the SDK's upload integrity checks.
  requestStreamBufferSize: 65_536,
});
