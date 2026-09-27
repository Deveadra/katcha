import assert from 'node:assert/strict';
import {
  createCloudAssetUrlResolver,
  stagingKeyFor,
} from '../src/cloud-media-staging.mjs';

const keyA = stagingKeyFor({
  prefix: 'tmp/render-inputs/',
  sourceBucket: 'katcha-media',
  sourceKey: 'raw/aa/example clip.mp4',
  sourceIdentity: 'etag-123:13:2026-09-26T00:00:00.000Z',
});
const keyB = stagingKeyFor({
  prefix: 'tmp/render-inputs',
  sourceBucket: 'katcha-media',
  sourceKey: 'raw/aa/example clip.mp4',
  sourceIdentity: 'etag-123:13:2026-09-26T00:00:00.000Z',
});

assert.equal(keyA, keyB);
assert.match(
  keyA,
  /^tmp\/render-inputs\/[a-f0-9]{2}\/[a-f0-9]{64}\/example_clip\.mp4$/,
);

const sourceCommands = [];
const cloudCommands = [];
const sourceClient = {
  async send(command) {
    sourceCommands.push(command);
    return {
      Body: Buffer.from('owned-fixture'),
      ContentLength: 13,
      ContentType: 'video/mp4',
      ETag: '"etag-123"',
      LastModified: new Date('2026-09-26T00:00:00.000Z'),
    };
  },
};
const cloudClient = {
  async send(command) {
    cloudCommands.push(command);
    if (command.constructor.name === 'HeadObjectCommand') {
      return {ContentLength: 13};
    }
    return {};
  },
};
const signer = async (_client, command, options) => {
  assert.equal(command.input.Bucket, 'katcha-render-staging-123456789012');
  assert.equal(options.expiresIn, 3600);
  return `https://katcha-render-staging-123456789012.s3.us-east-1.amazonaws.com/${command.input.Key}?signed=test`;
};

const resolveUrl = createCloudAssetUrlResolver({
  sourceClient,
  sourceBucket: 'katcha-media',
  region: 'us-east-1',
  stagingBucket: 'katcha-render-staging-123456789012',
  stagingPrefix: 'tmp/render-inputs',
  expiresInSeconds: 3600,
  cloudClient,
  signer,
});

const url = await resolveUrl('short-episode/example/audio/beat-00.wav');
assert.match(url, /^https:\/\/katcha-render-staging-/);
assert.equal(sourceCommands.length, 1);
assert.equal(sourceCommands[0].constructor.name, 'GetObjectCommand');
assert.equal(cloudCommands.length, 2);
assert.equal(cloudCommands[0].constructor.name, 'PutObjectCommand');
assert.equal(cloudCommands[1].constructor.name, 'HeadObjectCommand');
assert.equal(cloudCommands[0].input.ServerSideEncryption, 'AES256');
assert.equal(cloudCommands[0].input.ContentLength, 13);
assert.equal(cloudCommands[0].input.ContentType, 'video/mp4');
assert.equal(cloudCommands[0].input.ACL, undefined);
assert.ok(cloudCommands[0].input.Metadata['katcha-source-etag-sha256']);

const missingEtagResolver = createCloudAssetUrlResolver({
  sourceClient: {
    async send() {
      return {
        Body: Buffer.from('owned-fixture'),
        ContentLength: 13,
        ContentType: 'video/mp4',
      };
    },
  },
  sourceBucket: 'katcha-media',
  region: 'us-east-1',
  stagingBucket: 'katcha-render-staging-123456789012',
  stagingPrefix: 'tmp/render-inputs',
  expiresInSeconds: 3600,
  cloudClient,
  signer,
});
await assert.rejects(
  () => missingEtagResolver('short-episode/example/audio/beat-00.wav'),
  /without an ETag/,
);

console.log('PASS: cloud staging freezes, verifies, and privately signs render inputs.');
