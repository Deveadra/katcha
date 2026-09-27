import assert from 'node:assert/strict';
import fs from 'node:fs/promises';

const packageJson = JSON.parse(
  await fs.readFile(new URL('../package.json', import.meta.url), 'utf8'),
);

const expected = '4.0.529';
for (const dependency of [
  'remotion',
  '@remotion/bundler',
  '@remotion/cli',
  '@remotion/renderer',
  '@remotion/lambda',
]) {
  assert.equal(
    packageJson.dependencies[dependency],
    expected,
    `${dependency} must be pinned exactly to ${expected}`,
  );
}

console.log(`PASS: all Remotion packages are pinned exactly to ${expected}.`);
