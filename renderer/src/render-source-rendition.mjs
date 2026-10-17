import {createHash} from 'node:crypto';
import {execFile} from 'node:child_process';
import fs from 'node:fs';
import {mkdtemp, readFile, rm, stat} from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {pipeline} from 'node:stream/promises';
import {promisify} from 'node:util';
import {GetObjectCommand, HeadObjectCommand, PutObjectCommand} from '@aws-sdk/client-s3';

const exec = promisify(execFile);
const missing = (error) => ['NotFound', 'NoSuchKey'].includes(error?.name)
  || error?.$metadata?.httpStatusCode === 404;
const probe = async (file) => {
  const {stdout} = await exec('ffprobe', ['-v', 'error', '-show_streams', '-show_format',
    '-of', 'json', file], {timeout: 30000, maxBuffer: 1024 * 1024});
  return JSON.parse(stdout);
};

// A transport rendition leaves the stored original and frozen edit manifest intact.
// Decode costly sources once, instead of decoding 4K AV1 separately in every worker.
export const createRenderSourceResolver = ({sourceClient, sourceBucket, resolveAssetUrl,
  enabled = true}) => async (source, width, height) => {
  if (!enabled) return resolveAssetUrl(source.storage_key);
  if (![width, height].every(v => Number.isSafeInteger(v) && v >= 2 && v <= 7680)) {
    throw new Error('Invalid render source dimensions');
  }
  const key = source.storage_key;
  const original = await sourceClient.send(new HeadObjectCommand({Bucket: sourceBucket, Key: key}));
  if (!original.ETag || !(original.ContentLength > 0)) {
    throw new Error('Render source identity is unavailable');
  }
  const identity = createHash('sha256').update(JSON.stringify([
    'h264-crf18-audio-copy-v1', sourceBucket, key, original.ETag,
    original.ContentLength, String(original.LastModified || ''), width, height,
  ])).digest('hex');
  const preparedKey = `render-renditions/v1/${identity}.mp4`;
  try {
    const cached = await sourceClient.send(new HeadObjectCommand({Bucket: sourceBucket, Key: preparedKey}));
    if (cached.Metadata?.['katcha-source-identity'] !== identity || !(cached.ContentLength > 0)
      || !/^[a-f0-9]{64}$/.test(cached.Metadata?.['katcha-content-sha256'] || '')) {
      throw new Error('Cached render source identity does not match');
    }
    return resolveAssetUrl(preparedKey);
  } catch (error) {
    if (!missing(error)) throw error;
  }
  const folder = await mkdtemp(path.join(os.tmpdir(), 'katcha-render-source-'));
  try {
    const input = path.join(folder, 'original');
    const output = path.join(folder, 'prepared.mp4');
    const fetched = await sourceClient.send(new GetObjectCommand({Bucket: sourceBucket,
      Key: key, IfMatch: original.ETag}));
    if (!fetched.Body) throw new Error('Render source is unreadable');
    await pipeline(fetched.Body, fs.createWriteStream(input, {flags: 'wx'}));
    if ((await stat(input)).size !== Number(original.ContentLength)) {
      throw new Error('Render source download was incomplete');
    }
    const before = await probe(input);
    const video = before.streams?.find(s => s.codec_type === 'video');
    if (!video) throw new Error('Render source has no video stream');
    if (video.codec_name === 'h264' && video.width <= width && video.height <= height) {
      return resolveAssetUrl(key);
    }
    await exec('ffmpeg', ['-hide_banner', '-loglevel', 'error', '-nostdin', '-threads', '1', '-filter_threads', '1',
      '-i', input, '-map', '0:v:0', '-map', '0:a?', '-vf',
      `scale=w=${width}:h=${height}:force_original_aspect_ratio=decrease:force_divisible_by=2`,
      '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '18', '-pix_fmt', 'yuv420p',
      '-threads', '1', '-fps_mode', 'passthrough', '-c:a', 'copy', '-map_metadata', '-1',
      '-movflags', '+faststart', output], {timeout: 600000, maxBuffer: 1024 * 1024});
    const after = await probe(output);
    const prepared = after.streams?.find(s => s.codec_type === 'video');
    const audioBefore = before.streams.filter(s => s.codec_type === 'audio');
    const audioAfter = after.streams.filter(s => s.codec_type === 'audio');
    const beforeDuration = Number(before.format?.duration);
    const afterDuration = Number(after.format?.duration);
    if (prepared?.codec_name !== 'h264' || prepared.width > width || prepared.height > height
      || !(beforeDuration > 0) || !(afterDuration > 0)
      || Math.abs(beforeDuration - afterDuration) > 0.1
      || audioBefore.length !== audioAfter.length
      || audioBefore.some((a, i) => a.codec_name !== audioAfter[i].codec_name)
      || (video.nb_frames && prepared.nb_frames !== video.nb_frames)) {
      throw new Error('Prepared render source failed duration, frame, or audio verification');
    }
    const bytes = await readFile(output);
    await sourceClient.send(new PutObjectCommand({Bucket: sourceBucket, Key: preparedKey,
      Body: bytes, ContentLength: bytes.length, ContentType: 'video/mp4',
      Metadata: {'katcha-source-identity': identity,
        'katcha-content-sha256': createHash('sha256').update(bytes).digest('hex')}}));
    const uploaded = await sourceClient.send(new HeadObjectCommand({Bucket: sourceBucket, Key: preparedKey}));
    if (Number(uploaded.ContentLength) !== bytes.length
      || uploaded.Metadata?.['katcha-source-identity'] !== identity) {
      throw new Error('Prepared render source upload could not be verified');
    }
    console.log(`render source prepared codec=h264 width=${prepared.width} height=${prepared.height} audio=copy`);
    return resolveAssetUrl(preparedKey);
  } finally {
    await rm(folder, {recursive: true, force: true});
  }
};
