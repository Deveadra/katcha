import assert from 'node:assert/strict';
import {execFile} from 'node:child_process';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {promisify} from 'node:util';
import {bundle} from '@remotion/bundler';
import {renderStill, selectComposition} from '@remotion/renderer';

const exec = promisify(execFile);
const outputDir = path.resolve('thumbnail-smoke-previews');
const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), 'katcha-thumbnail-smoke-'));
const publicDir = path.join(tempDir, 'public');

try {
  await fs.mkdir(publicDir, {recursive: true});
  await fs.mkdir(outputDir, {recursive: true});
  await exec('ffmpeg', [
    '-hide_banner', '-loglevel', 'error', '-y',
    '-f', 'lavfi', '-i', 'testsrc2=s=1280x720:d=1',
    '-frames:v', '1', path.join(publicDir, 'thumbnail-source.png'),
  ]);
  const serveUrl = await bundle({
    entryPoint: path.resolve('test/thumbnail-smoke-entry.jsx'),
    publicDir,
    outDir: path.join(tempDir, 'bundle'),
  });
  const composition = await selectComposition({
    serveUrl, id: 'SyntheticThumbnail', inputProps: {},
  });
  const output = path.join(outputDir, 'SyntheticThumbnail.png');
  await renderStill({
    composition, serveUrl, frame: 0, imageFormat: 'png', output, inputProps: {},
  });
  const bytes = await fs.readFile(output);
  assert.ok(bytes.length > 1000);
  assert.equal(bytes[0], 0x89);
  assert.equal(bytes[1], 0x50);
  const {stdout} = await exec('ffprobe', [
    '-v', 'error', '-select_streams', 'v:0',
    '-show_entries', 'stream=width,height', '-of', 'json', output,
  ]);
  const probe = JSON.parse(stdout);
  assert.equal(probe.streams[0].width, 1280);
  assert.equal(probe.streams[0].height, 720);
  console.log('PASS: grounded branded thumbnail renders at YouTube dimensions.');
} finally {
  await fs.rm(tempDir, {recursive: true, force: true});
}