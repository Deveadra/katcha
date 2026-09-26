import assert from 'node:assert/strict';
import fs from 'node:fs/promises';

const source = await fs.readFile(
  new URL('../src/ranked-episode-video.jsx', import.meta.url),
  'utf8',
);

const decoderCount = (source.match(/<OffthreadVideo\b/g) || []).length;
assert.equal(
  decoderCount,
  1,
  `ranked episode must use one OffthreadVideo decoder per clip; found ${decoderCount}`,
);

assert.match(
  source,
  /objectFit:\s*'contain'/,
  'ranked foreground video must remain contained inside the branded canvas',
);

console.log('PASS: ranked episodes use a single source-video decoder per active clip.');
