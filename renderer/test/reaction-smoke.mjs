import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {execFile} from 'node:child_process';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {promisify} from 'node:util';
import {bundle} from '@remotion/bundler';
import {renderMedia, renderStill, selectComposition} from '@remotion/renderer';

const exec = promisify(execFile);
const outputDir = path.resolve('reaction-smoke-previews');
const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), 'katcha-reaction-smoke-'));
const publicDir = path.join(tempDir, 'public');
const fps = 30;
const frames = {before: 6, during: 30, after: 66};
const expectedReactionSha256 =
  'fc0cddb1e95245c757e01cc7363a5cdac221366598660606de5148cb966f8e2f';

function silenceWav() {
  const sampleRate = 16000;
  const sampleCount = sampleRate * 3;
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

async function still({serveUrl, id, frame, showReaction}) {
  const props = {showReaction};
  const composition = await selectComposition({serveUrl, id, inputProps: props});
  assert.equal(composition.props.showReaction, showReaction);
  const filename = path.join(outputDir, `${id}-${frame}-${showReaction ? 'on' : 'off'}.png`);
  await renderStill({
    composition,
    serveUrl,
    frame,
    imageFormat: 'png',
    output: filename,
    inputProps: props,
  });
  return fs.readFile(filename);
}

try {
  await fs.mkdir(publicDir, {recursive: true});
  await fs.mkdir(outputDir, {recursive: true});

  const reactionSource = path.resolve(
    'assets/ranksnaxx/reactions/host_emotes/v1/meme_cry.png',
  );
  const reactionBytes = await fs.readFile(reactionSource);
  const reactionSha256 = createHash('sha256').update(reactionBytes).digest('hex');
  assert.equal(reactionSha256, expectedReactionSha256);
  await fs.writeFile(path.join(publicDir, 'ranksnaxx-meme-cry.png'), reactionBytes);
  await fs.writeFile(path.join(publicDir, 'silence.wav'), silenceWav());

  await exec('ffmpeg', [
    '-hide_banner', '-loglevel', 'error', '-y',
    '-f', 'lavfi', '-i', 'color=c=0x294e64:s=540x960:r=30:d=2.7',
    '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
    '-an', path.join(publicDir, 'synthetic-source.mp4'),
  ]);

  const serveUrl = await bundle({
    entryPoint: path.resolve('test/reaction-smoke-entry.jsx'),
    publicDir,
    outDir: path.join(tempDir, 'bundle'),
  });

  for (const id of ['BrandedShort', 'BrandedRanked']) {
    const composition = await selectComposition({serveUrl, id, inputProps: {showReaction: true}});
    for (const [phase, frame] of Object.entries(frames)) {
      const [enabled, disabled] = await Promise.all([
        still({serveUrl, id, frame, showReaction: true}),
        still({serveUrl, id, frame, showReaction: false}),
      ]);
      if (phase === 'during') {
        assert.notDeepEqual(
          enabled, disabled, `${id}: reaction did not change an active frame`,
        );
      } else {
        assert.deepEqual(
          enabled, disabled, `${id}: reaction altered a frame outside its cue`,
        );
      }
      console.log(`${id} ${phase}: ${phase === 'during' ? 'sprite visible' : 'no sprite'}`);
    }

    const outputLocation = path.join(outputDir, `${id}.mp4`);
    await renderMedia({
      composition,
      serveUrl,
      codec: 'h264',
      outputLocation,
      inputProps: {showReaction: true},
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
    console.log(`${id}: encoded H.264 MP4, ${probe.format.duration}s`);
  }
  console.log(
    'PASS: both production compositions encode with the checksum-pinned RankSnaxx reaction asset.',
  );
} finally {
  await fs.rm(tempDir, {recursive: true, force: true});
}
