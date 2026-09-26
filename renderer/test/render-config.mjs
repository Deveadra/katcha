import assert from 'node:assert/strict';
import {resolveRenderSettings} from '../src/render-config.mjs';

assert.deepEqual(resolveRenderSettings({}), {
  concurrency: 1,
  timeoutInMilliseconds: 120000,
});

assert.deepEqual(
  resolveRenderSettings({
    KATCHA_RENDER_CONCURRENCY: '3',
    KATCHA_RENDER_TIMEOUT_MS: '180000',
  }),
  {
    concurrency: 3,
    timeoutInMilliseconds: 180000,
  },
);

assert.deepEqual(
  resolveRenderSettings({
    KATCHA_RENDER_CONCURRENCY: '0',
    KATCHA_RENDER_TIMEOUT_MS: '1000',
  }),
  {
    concurrency: 1,
    timeoutInMilliseconds: 120000,
  },
);

console.log('PASS: renderer runtime settings are conservative and configurable.');
