import assert from 'node:assert/strict';
import {assertCloudReachableInputProps} from '../src/lambda-renderer.mjs';

assert.doesNotThrow(() => assertCloudReachableInputProps({
  source: {
    url: 'https://katcha-media.s3.us-east-1.amazonaws.com/raw/example.mp4?X-Amz-Signature=test',
  },
  overlays: [{url: 'https://cdn.example.com/narration.wav'}],
}));

for (const url of [
  'http://minio:9000/katcha-media/source.mp4',
  'http://acceptance-media:8090/clip-1.mp4',
  'http://127.0.0.1:9000/source.mp4',
  'https://192.168.1.10/source.mp4',
]) {
  assert.throws(
    () => assertCloudReachableInputProps({source: {url}}),
    /Lambda (media URL must use HTTPS|cannot reach local\/private media host)/,
  );
}

console.log('PASS: Lambda renderer rejects local/private media URLs.');
