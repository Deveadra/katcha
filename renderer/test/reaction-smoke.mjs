import assert from 'node:assert/strict';
import {execFile} from 'node:child_process';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {promisify} from 'node:util';
import zlib from 'node:zlib';
import {bundle} from '@remotion/bundler';
import {renderMedia, renderStill, selectComposition} from '@remotion/renderer';

const exec = promisify(execFile);
const outputDir = path.resolve('reaction-smoke-previews');
const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), 'katcha-reaction-smoke-'));
const publicDir = path.join(tempDir, 'public');
const fps = 30;
const frames = {before: 6, during: 30, after: 66};

function crc32(buffer) {
  let crc = 0xffffffff;
  for (const byte of buffer) {
    crc ^= byte;
    for (let bit = 0; bit < 8; bit++) {
      crc = (crc >>> 1) ^ (crc & 1 ? 0xedb88320 : 0);
    }
  }
  return (crc ^ 0xffffffff) >>> 0;
}

function chunk(type, bytes) {
  const label = Buffer.from(type, 'ascii');
  const header = Buffer.alloc(4);
  header.writeUInt32BE(bytes.length);
  const checksum = Buffer.alloc(4);
  checksum.writeUInt32BE(crc32(Buffer.concat([label, bytes])));
  return Buffer.concat([header, label, bytes, checksum]);
}

// A generated 128x128 RGBA cartoon face, NOT a branded character asset.
function syntheticTransparentPng() {
  const size = 128;
  const pixels = Buffer.alloc(size * (size * 4 + 1));
  for (let y = 0; y < size; y++) {
    const row = y * (size * 4 + 1);
    for (let x = 0; x < size; x++) {
      const offset = row + 1 + x * 4;
      const face = ((x - 64) / 51) ** 2 + ((y - 61) / 49) ** 2 < 1;
      const eye = (((x - 45) / 6) ** 2 + ((y - 53) / 8) ** 2 < 1)
        || (((x - 83) / 6) ** 2 + ((y - 53) / 8) ** 2 < 1);
      const tear = (x > 38 && x < 51 || x > 77 && x < 90) && y > 63 && y < 102;
      const mouth = ((x - 64) / 15) ** 2 + ((y - 82) / 9) ** 2 < 1;
      const rgba = tear ? [80, 177, 255, 255]
        : (eye || mouth) ? [29, 29, 48, 255]
        : face ? [255, 201, 58, 255] : [0, 0, 0, 0];
      for (let channel = 0; channel < 4; channel++) {
        pixels[offset + channel] = rgba[channel];
      }
    }
  }
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(size, 0);
  ihdr.writeUInt32BE(size, 4);
  ihdr[8] = 8;
  ihdr[9] = 6; // RGBA
  return Buffer.concat([
    Buffer.from('89504e470d0a1a0a', 'hex'),
    chunk('IHDR', ihdr),
    chunk('IDAT', zlib.deflateSync(pixels)),
    chunk('IEND', Buffer.alloc(0)),
  ]);
}

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
  // selectComposition freezes inputProps in composition.props. Re-select for
  // each variant; passing a different flag to renderStill alone is not enough.
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
  await fs.writeFile(path.join(publicDir, 'test-only-reaction.png'), syntheticTransparentPng());
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

  for (const id of ['SyntheticShort', 'SyntheticRanked']) {
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
  console.log('PASS: both real compositions encode and obey reaction timing.');
} finally {
  await fs.rm(tempDir, {recursive: true, force: true});
}
