import {createHash} from 'node:crypto';
import path from 'node:path';
import {
  GetObjectCommand,
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
}) => {
  const digest = createHash('sha256')
    .update(`${sourceBucket}\0${sourceKey}`)
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
    if (!(size > 0) || !source.Body) {
      throw new Error(`cannot stage empty or unreadable render asset: ${sourceKey}`);
    }

    const stagedKey = stagingKeyFor({
      prefix: stagingPrefix,
      sourceBucket,
      sourceKey,
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
        },
      }),
    );

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
