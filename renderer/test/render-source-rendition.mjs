import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {createHash} from 'node:crypto';
import {mkdtemp, readFile, rm, writeFile} from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {Readable} from 'node:stream';
import {createRenderSourceResolver} from '../src/render-source-rendition.mjs';
const folder = await mkdtemp(path.join(os.tmpdir(), 'rendition-test-'));
try {
  const input = path.join(folder, 'input.mp4');
  execFileSync('ffmpeg', ['-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
    '-i', 'testsrc2=size=320x180:rate=24:duration=1', '-f', 'lavfi',
    '-i', 'sine=frequency=440:duration=1', '-c:v', 'libx264', '-threads', '1',
    '-c:a', 'aac', '-shortest', input]);
  const original = await readFile(input);
  const sourceKey = 'raw/channel/source.mp4';
  const objects = new Map([[sourceKey, {Body: original, ETag: 'original-v1',
    ContentLength: original.length, LastModified: new Date('2026-10-17T00:00:00Z')}]]);
  let downloads = 0;
  let puts = 0;
  const client = {send: async command => {
    const {Key} = command.input;
    if (command.constructor.name === 'PutObjectCommand') {
      puts++;
      const chunks = [];
      for await (const chunk of command.input.Body) chunks.push(chunk);
      objects.set(Key, {...command.input, Body: Buffer.concat(chunks), ETag: 'prepared-v1'});
      return {};
    }
    const value = objects.get(Key);
    if (!value) throw Object.assign(new Error('missing'), {name: 'NoSuchKey'});
    if (command.constructor.name === 'GetObjectCommand') {
      downloads++;
      assert.equal(command.input.IfMatch, value.ETag);
      return {...value, Body: Readable.from(value.Body)};
    }
    return value;
  }};
  const resolve = createRenderSourceResolver({sourceClient: client, sourceBucket: 'private',
    resolveAssetUrl: async key => key});
  const source = {storage_key: sourceKey, source_start_seconds: 0.25, audio_volume: 1};
  const frozen = JSON.stringify(source);
  const preparedKey = await resolve(source, 160, 160);
  assert.match(preparedKey, /^render-renditions\/v1\/[a-f0-9]{64}\.mp4$/);
  assert.equal(JSON.stringify(source), frozen);
  assert.deepEqual(objects.get(sourceKey).Body, original);
  assert.equal(downloads, 1);
  assert.equal(puts, 1);
  assert.equal(await resolve(source, 160, 160), preparedKey);
  assert.equal(downloads, 1, 'cached retry must not download/transcode again');
  const preparedFile = path.join(folder, 'prepared.mp4');
  await writeFile(preparedFile, objects.get(preparedKey).Body);
  const probe = JSON.parse(execFileSync('ffprobe', ['-v', 'error', '-show_streams', '-show_format', '-of', 'json', preparedFile]));
  assert.equal(probe.streams[0].width, 160);
  assert.equal(probe.streams[0].height, 90);
  assert.equal(probe.streams[0].nb_frames, '24');
  const audioHash = file => createHash('sha256').update(execFileSync('ffmpeg', ['-v', 'error',
    '-i', file, '-map', '0:a:0', '-c:a', 'copy', '-f', 'adts', '-'])).digest('hex');
  assert.equal(audioHash(input), audioHash(preparedFile), 'audio packets are copied exactly');
  const prepared = objects.get(preparedKey);
  prepared.Metadata['katcha-source-identity'] = 'wrong';
  await assert.rejects(resolve(source, 160, 160), /identity does not match/);
  assert.equal(downloads, 1);
  objects.delete(preparedKey);
  const originalUrl = await resolve(source, 320, 180);
  assert.equal(originalUrl, sourceKey, 'suitable H264 sources retain their original transport');
  assert.equal(puts, 1);
  const bypass = createRenderSourceResolver({enabled: false,
    sourceClient: {send: () => assert.fail('local render must not prepare media')},
    sourceBucket: 'private', resolveAssetUrl: async key => key});
  assert.equal(await bypass(source, 160, 160), sourceKey);
  console.log('render source rendition tests passed: real ffmpeg, frame/audio preservation, caching, identity, local bypass');
} finally {
  await rm(folder, {recursive: true, force: true});
}
