import assert from 'node:assert/strict';
import {execFile} from 'node:child_process';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {promisify} from 'node:util';
import {bundle} from '@remotion/bundler';
import {renderMedia, renderStill, selectComposition} from '@remotion/renderer';

const exec = promisify(execFile);
const outputDir = path.resolve('blueprint-smoke-previews');
const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), 'katcha-blueprint-smoke-'));
const publicDir = path.join(tempDir, 'public');

function silenceWav() {
  const sampleRate = 16000;
  const sampleCount = sampleRate * 2;
  const pcm = Buffer.alloc(sampleCount * 2);
  const header = Buffer.alloc(44);
  header.write('RIFF');
  header.writeUInt32LE(36 + pcm.length, 4);
  header.write('WAVEfmt ', 8);
  header.writeUInt32LE(16, 16);
  header.writeUInt16LE(1, 20);
  header.writeUInt16LE(1, 22);
  header.writeUInt32LE(sampleRate, 24);
  header.writeUInt32LE(sampleRate * 2, 28);
  header.writeUInt16LE(2, 32);
  header.writeUInt16LE(16, 34);
  header.write('data', 36);
  header.writeUInt32LE(pcm.length, 40);
  return Buffer.concat([header, pcm]);
}

try {
  await fs.mkdir(publicDir, {recursive: true});
  await fs.mkdir(outputDir, {recursive: true});
  await fs.writeFile(path.join(publicDir, 'silence.wav'), silenceWav());
  await exec('ffmpeg', [
    '-hide_banner', '-loglevel', 'error', '-y',
    '-f', 'lavfi', '-i', 'testsrc2=s=540x960:r=30:d=2.7',
    '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
    '-an', path.join(publicDir, 'synthetic-source.mp4'),
  ]);

  const serveUrl = await bundle({
    entryPoint: path.resolve('test/blueprint-smoke-entry.jsx'),
    publicDir,
    outDir: path.join(tempDir, 'bundle'),
  });

  const ids = ['SyntheticPersonaBlueprint', 'SyntheticHeaderBlueprint'];
  const stills = [];
  for (const id of ids) {
    const composition = await selectComposition({serveUrl, id, inputProps: {}});
    const stillPath = path.join(outputDir, `${id}.png`);
    await renderStill({
      composition,
      serveUrl,
      frame: 30,
      imageFormat: 'png',
      output: stillPath,
      inputProps: {},
    });
    stills.push(await fs.readFile(stillPath));

    const outputLocation = path.join(outputDir, `${id}.mp4`);
    await renderMedia({
      composition,
      serveUrl,
      codec: 'h264',
      outputLocation,
      inputProps: {},
      concurrency: 2,
    });
    const {stdout} = await exec('ffprobe', [
      '-v', 'error', '-select_streams', 'v:0',
      '-show_entries', 'stream=codec_name,width,height:format=duration',
      '-of', 'json', outputLocation,
    ]);
    const probe = JSON.parse(stdout);
    assert.equal(probe.streams[0].codec_name, 'h264');
    assert.equal(probe.streams[0].width, 540);
    assert.equal(probe.streams[0].height, 960);
    assert.ok(Number(probe.format.duration) >= 2.6);
    assert.ok(Number(probe.format.duration) <= 2.9);
  }

  assert.notDeepEqual(
    stills[0],
    stills[1],
    'persona and header blueprints unexpectedly rendered identical frames',
  );
  console.log('PASS: both editing blueprints encode and remain visually distinct.');
} finally {
  await fs.rm(tempDir, {recursive: true, force: true});
}
