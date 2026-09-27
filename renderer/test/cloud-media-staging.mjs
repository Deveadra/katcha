import assert from 'node:assert/strict';
import {
  createCloudAssetUrlResolver,
  stagingKeyFor,
} from '../src/cloud-media-staging.mjs';

const keyA = stagingKeyFor({
  prefix: 'tmp/render-inputs/',
  sourceBucket: 'katcha-media',
  sourceKey: 'raw/aa/example clip.mp4',
});
const keyB = stagingKeyFor({
  prefix: 'tmp/render-inputs',
  sourceBucket: 'katcha-media',
  sourceKey: 'raw/aa/example clip.mp4',
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
    };
  },
};
const cloudClient = {
  async send(command) {
    cloudCommands.push(command);
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
assert.equal(cloudCommands.length, 1);
assert.equal(cloudCommands[0].constructor.name, 'PutObjectCommand');
assert.equal(cloudCommands[0].input.ServerSideEncryption, 'AES256');
assert.equal(cloudCommands[0].input.ContentLength, 13);
assert.equal(cloudCommands[0].input.ContentType, 'video/mp4');
assert.equal(cloudCommands[0].input.ACL, undefined);

console.log('PASS: cloud staging is private, deterministic, and source-preserving.');
