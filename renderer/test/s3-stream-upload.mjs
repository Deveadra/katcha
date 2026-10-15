import assert from 'node:assert/strict';
import {Readable} from 'node:stream';
import {setImmediate} from 'node:timers/promises';
import {PutObjectCommand} from '@aws-sdk/client-s3';
import {createRenderS3Client} from '../src/s3-client.mjs';
import {createCloudAssetUrlResolver} from '../src/cloud-media-staging.mjs';

const payload = Buffer.alloc(200_003);
for (let i = 0; i < payload.length; i++) payload[i] = i % 251;

const fragmentedBody = (bytes) => Readable.from((async function* () {
  let offset = 0;
  const sizes = [1, 7, 8191, 3, 32768, 17, 2048];
  for (let i = 0; offset < bytes.length; i++) {
    const end = Math.min(offset + sizes[i % sizes.length], bytes.length);
    yield bytes.subarray(offset, end);
    offset = end;
    await setImmediate();
  }
})());

let uploads = 0;
let expectedPayload;
const client = createRenderS3Client({
  region: 'us-east-1',
  credentials: {accessKeyId: 'fixture', secretAccessKey: 'fixture'},
  maxAttempts: 1,
  requestHandler: {
    async handle(request) {
      if (request.method === 'HEAD') {
        return {response: {statusCode: 200, headers: {
          'content-length': String(expectedPayload.length),
        }, body: Readable.from([])}};
      }
      assert.equal(request.headers['content-encoding'], 'aws-chunked');
      assert.equal(request.headers['x-amz-trailer'], 'x-amz-checksum-crc32');
      const wire = [];
      for await (const chunk of request.body) wire.push(Buffer.from(chunk));
      const encoded = Buffer.concat(wire);
      const chunks = [];
      let offset = 0;
      while (true) {
        const lineEnd = encoded.indexOf('\r\n', offset);
        assert.ok(lineEnd >= offset);
        const size = Number.parseInt(encoded.subarray(offset, lineEnd).toString(), 16);
        offset = lineEnd + 2;
        if (size === 0) break;
        chunks.push(encoded.subarray(offset, offset + size));
        offset += size;
        assert.equal(encoded.subarray(offset, offset + 2).toString(), '\r\n');
        offset += 2;
      }
      // S3 rejects a short chunk followed by more data, even with ContentLength.
      for (const chunk of chunks.slice(0, -1)) assert.ok(chunk.length >= 8192);
      assert.deepEqual(Buffer.concat(chunks), expectedPayload);
      assert.match(encoded.subarray(offset).toString(), /x-amz-checksum-crc32:[A-Za-z0-9+/=]+/);
      uploads++;
      return {response: {statusCode: 200, headers: {}, body: Readable.from([])}};
    },
  },
});

try {
  // Exercise real AWS checksum middleware through the R2-to-S3 staging path.
  expectedPayload = payload;
  const resolveUrl = createCloudAssetUrlResolver({
    sourceClient: {async send() {
      return {Body: fragmentedBody(payload), ContentLength: payload.length, ETag: '"fixture"'};
    }},
    sourceBucket: 'source-fixture',
    stagingBucket: 'staging-fixture',
    cloudClient: client,
    signer: async () => 'https://fixture.invalid/private-render-input',
  });
  assert.equal(await resolveUrl('trailer.mp4'), 'https://fixture.invalid/private-render-input');

  // Small complete objects and the renderer's output upload use the same client.
  for (const bytes of [payload, Buffer.from('small asset')]) {
    expectedPayload = bytes;
    await client.send(new PutObjectCommand({
      Bucket: 'output-fixture', Key: 'render.mp4',
      Body: fragmentedBody(bytes), ContentLength: bytes.length,
    }));
  }
  assert.equal(uploads, 3);
} finally {
  client.destroy();
}
console.log('PASS: fragmented uploads retain bytes and checksum framing with bounded buffering.');
