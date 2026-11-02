import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {bundle} from '@remotion/bundler';
import {renderStill, selectComposition} from '@remotion/renderer';

// Render only the changed component: no source decoder or paid rendering service.
const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'katcha-meme-caption-'));
try {
  const serveUrl = await bundle({entryPoint: path.resolve('test/meme-caption-entry.jsx'),
    outDir: path.join(directory, 'bundle')});
  async function still(caption, frame, name) {
    const inputProps = {caption};
    const composition = await selectComposition({serveUrl, id: 'CaptionReaction', inputProps});
    const output = path.join(directory, name + '.png');
    await renderStill({serveUrl, composition, inputProps, output, frame, imageFormat: 'png'});
    return fs.readFile(output);
  }
  const first = await still('THE PLAN WENT SIDEWAYS', 30, 'first');
  const second = await still('ANOTHER ACTUAL CAPTION', 30, 'second');
  assert.notDeepEqual(first, second, 'Caption text must affect rendered pixels');
  const before = await still('THE PLAN WENT SIDEWAYS', 0, 'before');
  const after = await still('THE PLAN WENT SIDEWAYS', 59, 'after');
  assert.deepEqual(before, after, 'Reaction must disappear outside its planned interval');
  assert.notDeepEqual(first, before, 'Reaction must be visible during its planned interval');
  console.log('PASS: contextual caption pixels and event timing (synthetic fixture).');
} finally {
  await fs.rm(directory, {recursive: true, force: true});
}
