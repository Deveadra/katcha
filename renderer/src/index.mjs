import fs from 'node:fs';
import {execFile} from 'node:child_process';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {promisify} from 'node:util';
import express from 'express';
import {bundle} from '@remotion/bundler';
import {renderMedia, renderStill, selectComposition} from '@remotion/renderer';
import {
  GetObjectCommand,
  HeadObjectCommand,
  PutObjectCommand,
  S3Client,
} from '@aws-sdk/client-s3';
import {getSignedUrl} from '@aws-sdk/s3-request-presigner';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const bucket = process.env.KATCHA_S3_BUCKET || 'katcha-media';
const endpoint = process.env.KATCHA_S3_ENDPOINT_URL || 'http://minio:9000';
const region = process.env.KATCHA_S3_REGION || 'auto';
const port = Number(process.env.PORT || 8787);
const exec = promisify(execFile);

const s3 = new S3Client({
  endpoint,
  region,
  forcePathStyle: String(process.env.KATCHA_S3_FORCE_PATH_STYLE || 'true') === 'true',
  credentials: {
    accessKeyId: process.env.KATCHA_S3_ACCESS_KEY || 'katcha',
    secretAccessKey: process.env.KATCHA_S3_SECRET_KEY || 'katcha-local-secret',
  },
});

const entryPoint = path.resolve(__dirname, 'entry.jsx');
const serveUrlPromise = bundle({entryPoint});

const exists = async (key) => {
  try {
    await s3.send(new HeadObjectCommand({Bucket: bucket, Key: key}));
    return true;
  } catch (error) {
    const code = error?.name || error?.Code;
    if (code === 'NotFound' || code === 'NoSuchKey' || error?.$metadata?.httpStatusCode === 404) {
      return false;
    }
    throw error;
  }
};

const headObject = (key) => s3.send(new HeadObjectCommand({Bucket: bucket, Key: key}));

const inspectMedia = async (filePath) => {
  const {stdout} = await exec('ffprobe', [
    '-v', 'error',
    '-select_streams', 'v:0',
    '-show_entries', 'stream=width,height,r_frame_rate:format=duration',
    '-of', 'json',
    filePath,
  ]);
  const payload = JSON.parse(stdout);
  const stream = payload?.streams?.[0] || {};
  const duration = Number(payload?.format?.duration || 0);
  const width = Number(stream.width || 0);
  const height = Number(stream.height || 0);
  if (!(duration > 0) || !(width > 0) || !(height > 0)) {
    throw new Error('ffprobe could not verify rendered media');
  }
  return {duration_seconds: duration, width, height, frame_rate: stream.r_frame_rate || null};
};

const inspectImage = async (filePath) => {
  const {stdout} = await exec('ffprobe', [
    '-v', 'error',
    '-select_streams', 'v:0',
    '-show_entries', 'stream=width,height',
    '-of', 'json',
    filePath,
  ]);
  const payload = JSON.parse(stdout);
  const stream = payload?.streams?.[0] || {};
  const width = Number(stream.width || 0);
  const height = Number(stream.height || 0);
  if (!(width > 0) || !(height > 0)) {
    throw new Error('ffprobe could not verify rendered image');
  }
  return {width, height};
};

const verifyRender = (probe, manifest) => {
  const tolerance = Math.max(0.35, 2 / Number(manifest.fps || 30));
  if (Math.abs(probe.duration_seconds - Number(manifest.output_duration_seconds)) > tolerance) {
    throw new Error(
      `render duration mismatch: expected ${manifest.output_duration_seconds}s, got ${probe.duration_seconds}s`,
    );
  }
  if (probe.width !== Number(manifest.width) || probe.height !== Number(manifest.height)) {
    throw new Error(
      `render dimensions mismatch: expected ${manifest.width}x${manifest.height}, got ${probe.width}x${probe.height}`,
    );
  }
};

const signedGet = (key) =>
  getSignedUrl(s3, new GetObjectCommand({Bucket: bucket, Key: key}), {expiresIn: 3600});

const hydrateReactions = async (events = []) =>
  Promise.all(events.map(async (event) => {
    if (!(await exists(event.storage_key))) {
      throw new Error(`missing reaction asset: ${event.storage_key}`);
    }
    return {...event, url: await signedGet(event.storage_key)};
  }));

const hydrateShortManifest = async (manifest) => {
  const sourceUrl = await signedGet(manifest.source.storage_key);
  const overlays = await Promise.all(
    (manifest.overlays || []).map(async (overlay) => ({
      ...overlay,
      url: await signedGet(overlay.asset_key),
    })),
  );
  return {
    ...manifest,
    source: {...manifest.source, url: sourceUrl},
    overlays,
    reaction_events: await hydrateReactions(manifest.reaction_events),
  };
};

const hydrateBlueprintManifest = async (manifest) => {
  const source = {
    ...manifest.source,
    url: await signedGet(manifest.source.storage_key),
  };
  const narration = manifest.narration?.asset_key
    ? {...manifest.narration, url: await signedGet(manifest.narration.asset_key)}
    : null;
  return {...manifest, source, narration};
};

const hydrateRankedEpisodeManifest = async (manifest) => {
  const items = await Promise.all(
    (manifest.items || []).map(async (item) => ({
      ...item,
      source: {
        ...item.source,
        url: await signedGet(item.source.storage_key),
      },
    })),
  );
  const overlays = await Promise.all(
    (manifest.overlays || []).map(async (overlay) => ({
      ...overlay,
      url: await signedGet(overlay.asset_key),
    })),
  );
  return {...manifest, items, overlays, reaction_events: await hydrateReactions(manifest.reaction_events)};
};

const hydrateThumbnailManifest = async (manifest) => {
  if (!(await exists(manifest.source.storage_key))) {
    throw new Error(`missing thumbnail source asset: ${manifest.source.storage_key}`);
  }
  return {
    ...manifest,
    source: {
      ...manifest.source,
      url: await signedGet(manifest.source.storage_key),
    },
  };
};

const hydrateLongformManifest = async (manifest) => {
  const timeline = await Promise.all(
    (manifest.timeline || []).map(async (item) => {
      if (item.kind === 'clip' && item.clip?.storage_key) {
        return {
          ...item,
          clip: {
            ...item.clip,
            url: await signedGet(item.clip.storage_key),
          },
        };
      }
      if (item.kind === 'narration' && item.narration?.asset_key) {
        const backgroundUrl = item.narration.background_key
          ? await signedGet(item.narration.background_key)
          : null;
        return {
          ...item,
          narration: {
            ...item.narration,
            url: await signedGet(item.narration.asset_key),
            background_url: backgroundUrl,
          },
        };
      }
      return item;
    }),
  );
  return {...manifest, timeline};
};

const app = express();
app.use(express.json({limit: '8mb'}));

app.get('/health', (_request, response) => {
  response.json({status: 'ok', service: 'katcha-renderer'});
});

app.post('/thumbnail', async (request, response) => {
  const manifest = request.body;
  if (
    manifest?.version !== 'thumbnail-render-v1'
    || !manifest?.publication_id
    || !manifest?.parent_variant_id
    || !manifest?.source?.storage_key
    || !manifest?.output_key
  ) {
    return response.status(400).json({error: 'invalid thumbnail render manifest'});
  }

  try {
    if (await exists(manifest.output_key)) {
      const stored = await headObject(manifest.output_key);
      if (!(Number(stored.ContentLength || 0) > 0)) {
        throw new Error('existing thumbnail object is empty');
      }
      return response.json({
        output_key: manifest.output_key,
        width: Number(manifest.width || 1280),
        height: Number(manifest.height || 720),
        metadata: {
          reused: true,
          verified: true,
          verification_mode: 'object-head',
          object_size_bytes: Number(stored.ContentLength || 0),
          renderer: 'remotion',
          composition: 'Thumbnail',
        },
      });
    }

    const inputProps = await hydrateThumbnailManifest(manifest);
    const serveUrl = await serveUrlPromise;
    const composition = await selectComposition({
      serveUrl,
      id: 'Thumbnail',
      inputProps,
    });
    const tempDir = await fs.promises.mkdtemp(path.join(os.tmpdir(), 'katcha-thumbnail-'));
    const outputPath = path.join(tempDir, 'thumbnail.png');

    try {
      await renderStill({
        composition,
        serveUrl,
        output: outputPath,
        inputProps,
        imageFormat: 'png',
      });
      const bytes = await fs.promises.readFile(outputPath);
      if (
        bytes.length < 8
        || bytes[0] !== 0x89
        || bytes[1] !== 0x50
        || bytes[2] !== 0x4e
        || bytes[3] !== 0x47
      ) {
        throw new Error('thumbnail renderer did not produce a PNG');
      }
      const probe = await inspectImage(outputPath);
      if (
        probe.width !== Number(manifest.width || 1280)
        || probe.height !== Number(manifest.height || 720)
      ) {
        throw new Error(
          `thumbnail dimensions mismatch: expected ${manifest.width}x${manifest.height}, `
          + `got ${probe.width}x${probe.height}`,
        );
      }
      await s3.send(
        new PutObjectCommand({
          Bucket: bucket,
          Key: manifest.output_key,
          Body: bytes,
          ContentType: 'image/png',
          Metadata: {
            'katcha-render-version': 'thumbnail-render-v1',
            'katcha-verified': 'true',
          },
        }),
      );
      const stored = await headObject(manifest.output_key);
      if (!(Number(stored.ContentLength || 0) > 0)) {
        throw new Error('thumbnail upload verification returned an empty object');
      }
      return response.json({
        output_key: manifest.output_key,
        width: probe.width,
        height: probe.height,
        metadata: {
          reused: false,
          verified: true,
          verification_mode: 'png+ffprobe+object-head',
          object_size_bytes: Number(stored.ContentLength || 0),
          renderer: 'remotion',
          composition: 'Thumbnail',
        },
      });
    } finally {
      await fs.promises.rm(tempDir, {recursive: true, force: true});
    }
  } catch (error) {
    console.error('thumbnail render failed', error);
    return response.status(500).json({error: String(error?.message || error)});
  }
});

app.post('/render', async (request, response) => {
  const manifest = request.body;
  const isLongform = manifest?.version === 'longform-render-v1';
  const isRankedEpisode = manifest?.version === 'ranked-episode-render-v1';
  const isBlueprint = manifest?.version === 'blueprint-render-v1';
  const identity = isLongform
    ? manifest?.compilation_id
    : isRankedEpisode
      ? manifest?.short_episode_id
      : isBlueprint
        ? manifest?.render_id
        : manifest?.production_id;
  if (!identity || !manifest?.output_key) {
    return response.status(400).json({error: 'invalid render manifest'});
  }
  if (!isLongform && !isRankedEpisode && !manifest?.source?.storage_key) {
    return response.status(400).json({error: 'render manifest is missing source'});
  }
  if (isBlueprint && !manifest?.blueprint_key) {
    return response.status(400).json({error: 'blueprint render manifest is missing lineage'});
  }
  if (isRankedEpisode && (!Array.isArray(manifest?.items) || manifest.items.length < 3)) {
    return response.status(400).json({error: 'ranked episode manifest is missing items'});
  }
  if (isLongform && !Array.isArray(manifest?.timeline)) {
    return response.status(400).json({error: 'long-form render manifest is missing timeline'});
  }

  try {
    const compositionId = isLongform
      ? 'Longform'
      : isRankedEpisode
        ? 'RankedEpisode'
        : isBlueprint
          ? 'BlueprintVideo'
          : 'Short';
    if (await exists(manifest.output_key)) {
      const stored = await headObject(manifest.output_key);
      if (!(Number(stored.ContentLength || 0) > 0)) {
        throw new Error('existing render object is empty');
      }
      return response.json({
        output_key: manifest.output_key,
        duration_seconds: manifest.output_duration_seconds,
        metadata: {
          reused: true,
          verified: true,
          verification_mode: 'object-head',
          object_size_bytes: Number(stored.ContentLength || 0),
          renderer: 'remotion',
          composition: compositionId,
        },
      });
    }

    const inputProps = isLongform
      ? await hydrateLongformManifest(manifest)
      : isRankedEpisode
        ? await hydrateRankedEpisodeManifest(manifest)
        : isBlueprint
          ? await hydrateBlueprintManifest(manifest)
          : await hydrateShortManifest(manifest);
    const serveUrl = await serveUrlPromise;
    const composition = await selectComposition({
      serveUrl,
      id: compositionId,
      inputProps,
    });
    const tempDir = await fs.promises.mkdtemp(path.join(os.tmpdir(), 'katcha-render-'));
    const outputPath = path.join(
      tempDir,
      isLongform
        ? 'longform.mp4'
        : isRankedEpisode
          ? 'ranked-episode.mp4'
          : isBlueprint
            ? 'blueprint.mp4'
            : 'short.mp4',
    );

    try {
      await renderMedia({
        composition,
        serveUrl,
        codec: 'h264',
        outputLocation: outputPath,
        inputProps,
        concurrency: 2,
      });
      const probe = await inspectMedia(outputPath);
      verifyRender(probe, manifest);
      await s3.send(
        new PutObjectCommand({
          Bucket: bucket,
          Key: manifest.output_key,
          Body: fs.createReadStream(outputPath),
          ContentType: 'video/mp4',
          Metadata: {
            'katcha-render-version': String(manifest.version || 'unknown'),
            'katcha-verified': 'true',
          },
        }),
      );
      const stored = await headObject(manifest.output_key);
      if (!(Number(stored.ContentLength || 0) > 0)) {
        throw new Error('render upload verification returned an empty object');
      }
      inputProps.__renderVerification = {
        ...probe,
        object_size_bytes: Number(stored.ContentLength || 0),
      };
    } finally {
      await fs.promises.rm(tempDir, {recursive: true, force: true});
    }

    return response.json({
      output_key: manifest.output_key,
      duration_seconds: manifest.output_duration_seconds,
      metadata: {
        reused: false,
        verified: true,
        verification_mode: 'ffprobe+object-head',
        renderer: 'remotion',
        composition: compositionId,
        fps: manifest.fps,
        width: manifest.width,
        height: manifest.height,
        ...(inputProps.__renderVerification || {}),
      },
    });
  } catch (error) {
    console.error('render failed', error);
    return response.status(500).json({error: String(error?.message || error)});
  }
});

app.listen(port, '0.0.0.0', () => {
  console.log(`katcha renderer listening on ${port}`);
});
