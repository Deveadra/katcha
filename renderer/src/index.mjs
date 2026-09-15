import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import express from 'express';
import {bundle} from '@remotion/bundler';
import {renderMedia, selectComposition} from '@remotion/renderer';
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

const signedGet = (key) =>
  getSignedUrl(s3, new GetObjectCommand({Bucket: bucket, Key: key}), {expiresIn: 3600});

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
app.use(express.json({limit: '6mb'}));

app.get('/health', (_request, response) => {
  response.json({status: 'ok', service: 'katcha-renderer'});
});

app.post('/render', async (request, response) => {
  const manifest = request.body;
  const isLongform = manifest?.version === 'longform-render-v1';
  const identity = isLongform ? manifest?.compilation_id : manifest?.production_id;
  if (!identity || !manifest?.output_key) {
    return response.status(400).json({error: 'invalid render manifest'});
  }
  if (!isLongform && !manifest?.source?.storage_key) {
    return response.status(400).json({error: 'short render manifest is missing source'});
  }
  if (isLongform && !Array.isArray(manifest?.timeline)) {
    return response.status(400).json({error: 'long-form render manifest is missing timeline'});
  }

  try {
    if (await exists(manifest.output_key)) {
      return response.json({
        output_key: manifest.output_key,
        duration_seconds: manifest.output_duration_seconds,
        metadata: {
          reused: true,
          renderer: 'remotion',
          composition: isLongform ? 'Longform' : 'Short',
        },
      });
    }

    const inputProps = isLongform
      ? await hydrateLongformManifest(manifest)
      : await hydrateShortManifest(manifest);
    const serveUrl = await serveUrlPromise;
    const compositionId = isLongform ? 'Longform' : 'Short';
    const composition = await selectComposition({
      serveUrl,
      id: compositionId,
      inputProps,
    });
    const tempDir = await fs.promises.mkdtemp(path.join(os.tmpdir(), 'katcha-render-'));
    const outputPath = path.join(tempDir, isLongform ? 'longform.mp4' : 'short.mp4');

    try {
      await renderMedia({
        composition,
        serveUrl,
        codec: 'h264',
        outputLocation: outputPath,
        inputProps,
        concurrency: 2,
      });
      await s3.send(
        new PutObjectCommand({
          Bucket: bucket,
          Key: manifest.output_key,
          Body: fs.createReadStream(outputPath),
          ContentType: 'video/mp4',
        }),
      );
    } finally {
      await fs.promises.rm(tempDir, {recursive: true, force: true});
    }

    return response.json({
      output_key: manifest.output_key,
      duration_seconds: manifest.output_duration_seconds,
      metadata: {
        reused: false,
        renderer: 'remotion',
        composition: compositionId,
        fps: manifest.fps,
        width: manifest.width,
        height: manifest.height,
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
