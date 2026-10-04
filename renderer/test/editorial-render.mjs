// Actual synthetic media acceptance: no provider, S3, rights, or publication claims.
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {execFileSync} from 'node:child_process';
import {bundle} from '@remotion/bundler';
import {renderMedia, selectComposition} from '@remotion/renderer';
import {fixture} from './editorial.mjs';
import {validateEditorialManifest} from '../src/editorial-contract.mjs';
const temp = await fs.mkdtemp(path.join(os.tmpdir(), 'katcha-editorial-test-'));
try {
  const publicDir = path.join(temp, 'public'); await fs.mkdir(publicDir);
  execFileSync('ffmpeg', ['-y', '-f', 'lavfi', '-i', 'testsrc2=size=640x360:rate=30', '-t', '4', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', path.join(publicDir, 'test.mp4')], {stdio: 'ignore'});
  const serveUrl = await bundle({entryPoint: path.resolve('src/entry.jsx'), publicDir});
  const props = structuredClone(fixture);
  const comparison = structuredClone(props.timeline[0]);
  comparison.beat_id = 'comparison'; comparison.start_frame = 60; comparison.layout = 'comparison';
  comparison.media.push({...comparison.media[0], start_seconds: 3, freeze: true, push_in: 1});
  comparison.overlays.push({kind: 'arrow', media_index: 1, region: {x: 0.1, y: 0.1, width: 0.4, height: 0.4}, label: 'Frozen frame'});
  const quote = {...structuredClone(props.timeline[0]), beat_id: 'quote', start_frame: 120, layout: 'quote', media: [], overlays: [], quote_source_id: 'evidence', quote_text: 'Synthetic evidence quotation for layout verification.', source_credit: 'Synthetic source'};
  props.timeline.push(comparison, quote); props.output_duration_seconds = 6;
  validateEditorialManifest(props);
  props.media[0].url = '/public/test.mp4';
  const options = process.env.CHROMIUM_PATH ? {browserExecutable: process.env.CHROMIUM_PATH} : {};
  const composition = await selectComposition({serveUrl, id: 'Editorial', inputProps: props, ...options});
  const output = path.resolve('test-results/editorial-synthetic.mp4'); await fs.mkdir(path.dirname(output), {recursive: true});
  await renderMedia({serveUrl, composition, inputProps: props, codec: 'h264', outputLocation: output, concurrency: 1, ...options});
  const probe = JSON.parse(execFileSync('ffprobe', ['-v', 'error', '-show_streams', '-show_format', '-of', 'json', output]));
  const video = probe.streams.find(s => s.codec_type === 'video');
  if (video.width !== 1920 || video.height !== 1080 || Number(video.nb_frames) !== 180 || probe.streams.some(s => s.codec_type === 'audio')) throw new Error('Synthetic render verification failed');
  execFileSync('ffmpeg', ['-y', '-ss', '1', '-i', output, '-frames:v', '1', path.resolve('test-results/editorial-synthetic.png')], {stdio: 'ignore'});
  for (const [seconds, name] of [[3, 'comparison'], [5, 'quote']]) {
    execFileSync('ffmpeg', ['-y', '-ss', String(seconds), '-i', output, '-frames:v', '1', path.resolve(`test-results/editorial-${name}.png`)], {stdio: 'ignore'});
  }
  console.log('Verified synthetic editorial render: 1920x1080, 180 frames, silent; single, comparison, freeze and quote scenes');
} finally { await fs.rm(temp, {recursive: true, force: true}); }
