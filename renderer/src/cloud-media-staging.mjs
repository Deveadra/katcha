import {createHash} from 'node:crypto';
import path from 'node:path';
import {
  GetObjectCommand,
  HeadObjectCommand,
  PutObjectCommand,
  S3Client,
} from '@aws-sdk/client-s3';
import {getSignedUrl} from '@aws-sdk/s3-request-presigner';

const safeBasename = (key) => {
  const candidate = path.posix.basename(String(key || '')).replace(/[^A-Za-z0-9._-]/g, '_');
  return candidate || 'asset';
};

export const stagingKeyFor = ({
  prefix,
  sourceBucket,
  sourceKey,
  sourceIdentity,
}) => {
  const digest = createHash('sha256')
    .update(`${sourceBucket}\0${sourceKey}\0${sourceIdentity}`)
    .digest('hex');
  const normalizedPrefix = String(prefix || 'katcha-render-staging')
    .replace(/^\/+|\/+$/g, '');
  return `${normalizedPrefix}/${digest.slice(0, 2)}/${digest}/${safeBasename(sourceKey)}`;
};

export const createCloudAssetUrlResolver = ({
  sourceClient,
  sourceBucket,
  region,
  stagingBucket,
  stagingPrefix,
  expiresInSeconds,
  cloudClient = null,
  signer = getSignedUrl,
}) => {
  if (!sourceClient) {
    throw new Error('source S3 client is required for cloud staging');
  }
  if (!sourceBucket) {
    throw new Error('source bucket is required for cloud staging');
  }
  if (!stagingBucket) {
    throw new Error('staging bucket is required for cloud staging');
  }

  const destinationClient = cloudClient || new S3Client({region});

  return async (sourceKey) => {
    const source = await sourceClient.send(
      new GetObjectCommand({
        Bucket: sourceBucket,
        Key: sourceKey,
      }),
    );
    const size = Number(source.ContentLength || 0);
    const etag = String(source.ETag || '').replaceAll('"', '').trim();
    const lastModified = source.LastModified instanceof Date
      ? source.LastModified.toISOString()
      : String(source.LastModified || '').trim();
    if (!(size > 0) || !source.Body) {
      throw new Error(`cannot stage empty or unreadable render asset: ${sourceKey}`);
    }
    if (!etag) {
      throw new Error(`cannot freeze render asset without an ETag: ${sourceKey}`);
    }
    const sourceIdentity = `${etag}:${size}:${lastModified || 'unknown-time'}`;

    const stagedKey = stagingKeyFor({
      prefix: stagingPrefix,
      sourceBucket,
      sourceKey,
      sourceIdentity,
    });

    await destinationClient.send(
      new PutObjectCommand({
        Bucket: stagingBucket,
        Key: stagedKey,
        Body: source.Body,
        ContentLength: size,
        ContentType: source.ContentType || 'application/octet-stream',
        ServerSideEncryption: 'AES256',
        Metadata: {
          'katcha-source-key-sha256': createHash('sha256')
            .update(String(sourceKey))
            .digest('hex'),
          'katcha-source-etag-sha256': createHash('sha256')
            .update(etag)
            .digest('hex'),
        },
      }),
    );

    const staged = await destinationClient.send(
      new HeadObjectCommand({
        Bucket: stagingBucket,
        Key: stagedKey,
      }),
    );
    if (Number(staged.ContentLength || 0) !== size) {
      throw new Error(
        `staged render asset verification failed for ${sourceKey}: expected ${size} bytes`,
      );
    }

    return signer(
      destinationClient,
      new GetObjectCommand({
        Bucket: stagingBucket,
        Key: stagedKey,
      }),
      {expiresIn: expiresInSeconds},
    );
  };
};
