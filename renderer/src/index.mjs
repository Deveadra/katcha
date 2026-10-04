import {createHash} from 'node:crypto';
import {validateEditorialManifest} from './editorial-contract.mjs';
import {verifyNarrationBytes} from './editorial-media.mjs';
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
import {verifyExpectedAwsIdentity} from './aws-identity.mjs';
import {createCloudAssetUrlResolver} from './cloud-media-staging.mjs';
import {renderMediaViaLambda} from './lambda-renderer.mjs';
import {resolveRenderSettings, validateLambdaSettings} from './render-config.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const bucket = process.env.KATCHA_S3_BUCKET || 'katcha-media';
const hasEndpointSetting = Object.prototype.hasOwnProperty.call(
  process.env,
  'KATCHA_S3_ENDPOINT_URL',
);
const rawEndpoint = String(process.env.KATCHA_S3_ENDPOINT_URL ?? '').trim();
const endpoint = hasEndpointSetting
  ? (rawEndpoint || undefined)
  : 'http://minio:9000';
const region = process.env.KATCHA_S3_REGION || 'auto';
const port = Number(process.env.PORT || 8787);
const renderSettings = resolveRenderSettings();
validateLambdaSettings(renderSettings);
const exec = promisify(execFile);

let verifiedAwsIdentity = null;
if (renderSettings.backend === 'lambda') {
  verifiedAwsIdentity = await verifyExpectedAwsIdentity({
    expectedAccountId: renderSettings.lambda.expectedAccountId,
    region: renderSettings.lambda.region,
  });
  console.log(
    `AWS identity verified for Lambda rendering account=${verifiedAwsIdentity.account} `
    + `caller=${verifiedAwsIdentity.arn || 'unknown'}`,
  );
}

const s3AccessKey = String(process.env.KATCHA_S3_ACCESS_KEY ?? '').trim();
const s3SecretKey = String(process.env.KATCHA_S3_SECRET_KEY ?? '').trim();
const s3Options = {
  endpoint,
  region,
  forcePathStyle: String(process.env.KATCHA_S3_FORCE_PATH_STYLE || 'true') === 'true',
};
if (s3AccessKey && s3SecretKey) {
  s3Options.credentials = {
    accessKeyId: s3AccessKey,
    secretAccessKey: s3SecretKey,
  };
}
const s3 = new S3Client(s3Options);

const entryPoint = path.resolve(__dirname, 'entry.jsx');
let localServeUrlPromise = null;
const getLocalServeUrl = () => {
  if (localServeUrlPromise === null) {
    localServeUrlPromise = bundle({entryPoint});
  }
  return localServeUrlPromise;
};

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
    '-show_entries', 'stream=codec_type,width,height,r_frame_rate:format=duration',
    '-of', 'json',
    filePath,
  ]);
  const payload = JSON.parse(stdout);
  const stream = payload?.streams?.find(item => item.codec_type === 'video') || {};
  const duration = Number(payload?.format?.duration || 0);
  const width = Number(stream.width || 0);
  const height = Number(stream.height || 0);
  if (!(duration > 0) || !(width > 0) || !(height > 0)) {
    throw new Error('ffprobe could not verify rendered media');
  }
  return {duration_seconds: duration, width, height, frame_rate: stream.r_frame_rate || null,
    has_audio: payload?.streams?.some(item => item.codec_type === 'audio') || false};
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
  if (manifest.version === 'editorial-render-v2' && !probe.has_audio) throw new Error('Narrated editorial output is missing its audio track');
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

const renderAssetUrl = (
  renderSettings.backend === 'lambda' && renderSettings.lambda.stagingBucket
)
  ? createCloudAssetUrlResolver({
      sourceClient: s3,
      sourceBucket: bucket,
      region: renderSettings.lambda.region,
      stagingBucket: renderSettings.lambda.stagingBucket,
      stagingPrefix: renderSettings.lambda.stagingPrefix,
      expiresInSeconds: renderSettings.lambda.stagingUrlExpiresSeconds,
    })
  : signedGet;

const hydrateReactions = async (events = []) =>
  Promise.all(events.map(async (event) => {
    if (!(await exists(event.storage_key))) {
      throw new Error(`missing reaction asset: ${event.storage_key}`);
    }
    return {...event, url: await renderAssetUrl(event.storage_key)};
  }));

const hydrateBrand = async (brand) => {
  const logo = brand?.logo;
  if (!logo?.enabled || !logo?.storage_key) return brand;
  if (!(await exists(logo.storage_key))) {
    throw new Error(`missing channel logo asset: ${logo.storage_key}`);
  }
  return {
    ...brand,
    logo: {...logo, url: await renderAssetUrl(logo.storage_key)},
  };
};

const hydrateShortManifest = async (manifest) => {
  const sourceUrl = await renderAssetUrl(manifest.source.storage_key);
  const overlays = await Promise.all(
    (manifest.overlays || []).map(async (overlay) => ({
      ...overlay,
      url: await renderAssetUrl(overlay.asset_key),
    })),
  );
  return {
    ...manifest,
    source: {...manifest.source, url: sourceUrl},
    overlays,
    brand: await hydrateBrand(manifest.brand),
    reaction_events: await hydrateReactions(manifest.reaction_events),
  };
};

const hydrateBlueprintManifest = async (manifest) => {
  const source = {
    ...manifest.source,
    url: await renderAssetUrl(manifest.source.storage_key),
  };
  const narration = manifest.narration?.asset_key
    ? {...manifest.narration, url: await renderAssetUrl(manifest.narration.asset_key)}
    : null;
  return {...manifest, source, narration, brand: await hydrateBrand(manifest.brand)};
};

const hydrateRankedEpisodeManifest = async (manifest) => {
  const items = await Promise.all(
    (manifest.items || []).map(async (item) => ({
      ...item,
      source: {
        ...item.source,
        url: await renderAssetUrl(item.source.storage_key),
      },
    })),
  );
  const overlays = await Promise.all(
    (manifest.overlays || []).map(async (overlay) => ({
      ...overlay,
      url: await renderAssetUrl(overlay.asset_key),
    })),
  );
  return {
    ...manifest,
    items,
    overlays,
    brand: await hydrateBrand(manifest.brand),
    reaction_events: await hydrateReactions(manifest.reaction_events),
  };
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
            url: await renderAssetUrl(item.clip.storage_key),
          },
        };
      }
      if (item.kind === 'narration' && item.narration?.asset_key) {
        const backgroundUrl = item.narration.background_key
          ? await renderAssetUrl(item.narration.background_key)
          : null;
        return {
          ...item,
          narration: {
            ...item.narration,
            url: await renderAssetUrl(item.narration.asset_key),
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
  response.json({
    status: 'ok',
    service: 'katcha-renderer',
    render_backend: renderSettings.backend,
    lambda_region: renderSettings.backend === 'lambda' ? renderSettings.lambda.region : null,
    aws_account_id: verifiedAwsIdentity?.account || null,
    cloud_staging_enabled: Boolean(
      renderSettings.backend === 'lambda' && renderSettings.lambda.stagingBucket,
    ),
  });
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
    const serveUrl = await getLocalServeUrl();
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

app.post('/editorial-output', async (request, response) => {
  try {
    const manifest = validateEditorialManifest(request.body);
    const digest = createHash('sha256').update(JSON.stringify(manifest)).digest('hex');
    if (!(await exists(manifest.output_key))) return response.status(404).json({error: 'Editorial output is not available yet'});
    const stored = await headObject(manifest.output_key);
    if (!(Number(stored.ContentLength) > 0) || stored.Metadata?.['katcha-editorial-digest'] !== digest || stored.Metadata?.['katcha-verified'] !== 'true') {
      return response.status(409).json({error: 'Editorial output verification does not match'});
    }
    return response.json({output_key: manifest.output_key, duration_seconds: manifest.output_duration_seconds,
      metadata: {verified: true, reused: true, composition: 'Editorial', verification_mode: 'object-head+manifest-digest', object_size_bytes: Number(stored.ContentLength)}});
  } catch (error) {
    return response.status(400).json({error: String(error.message)});
  }
});

const activeEditorialOutputs = new Set();

app.post('/render', async (request, response) => {
  const manifest = request.body;
  const isEditorial = ['editorial-render-v1', 'editorial-render-v2'].includes(manifest?.version);
  let editorialDigest;
  if (isEditorial) {
    try {
      validateEditorialManifest(manifest);
      // Until cost authorization is connected to durable runs, editorial is local only.
      if (renderSettings.backend !== 'local') throw new Error('Editorial rendering requires the local backend');
      editorialDigest = createHash('sha256').update(JSON.stringify(manifest)).digest('hex');
    } catch (error) {
      return response.status(400).json({error: error.message});
    }
  }
  const isLongform = manifest?.version === 'longform-render-v1';
  const isRankedEpisode = manifest?.version === 'ranked-episode-render-v1';
  const isBlueprint = manifest?.version === 'blueprint-render-v1';
  const identity = isEditorial ? manifest.project_id : isLongform
    ? manifest?.compilation_id
    : isRankedEpisode
      ? manifest?.short_episode_id
      : isBlueprint
        ? manifest?.render_id
        : manifest?.production_id;
  if (!identity || !manifest?.output_key) {
    return response.status(400).json({error: 'invalid render manifest'});
  }
  if (!isEditorial && !isLongform && !isRankedEpisode && !manifest?.source?.storage_key) {
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

  if (isEditorial && activeEditorialOutputs.has(manifest.output_key)) {
    return response.status(409).json({error: 'Editorial render is already in progress'});
  }
  if (isEditorial) activeEditorialOutputs.add(manifest.output_key);
  try {
    const compositionId = isEditorial ? 'Editorial' : isLongform
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
      if (isEditorial && (stored.Metadata?.['katcha-editorial-digest'] !== editorialDigest || stored.Metadata?.['katcha-verified'] !== 'true')) {
        throw new Error('Existing editorial output does not match this verified manifest');
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

    const inputProps = isEditorial
      ? {...manifest, media: await Promise.all(manifest.media.map(async asset => {
          if (!(await exists(asset.storage_key))) throw new Error('Missing editorial media');
          return {...asset, url: await renderAssetUrl(asset.storage_key)};
        })), ...(manifest.version === 'editorial-render-v2' ? {narration: await Promise.all(manifest.narration.map(async audio => {
          const stored = await headObject(audio.storage_key);
          if (!(Number(stored.ContentLength) > 0) || Number(stored.ContentLength) > 32 * 1024 * 1024) throw new Error('Missing or oversized editorial narration');
          const object = await s3.send(new GetObjectCommand({Bucket: bucket, Key: audio.storage_key}));
          await verifyNarrationBytes(object.Body, audio.sha256);
          return {...audio, url: await renderAssetUrl(audio.storage_key)};
        }))} : {})}
      : isLongform
      ? await hydrateLongformManifest(manifest)
      : isRankedEpisode
        ? await hydrateRankedEpisodeManifest(manifest)
        : isBlueprint
          ? await hydrateBlueprintManifest(manifest)
          : await hydrateShortManifest(manifest);
    const serveUrl = renderSettings.backend === 'local'
      ? await getLocalServeUrl()
      : null;
    const composition = serveUrl
      ? await selectComposition({
          serveUrl,
          id: compositionId,
          inputProps,
        })
      : null;
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
      let backendMetadata = {};
      if (renderSettings.backend === 'lambda') {
        backendMetadata = await renderMediaViaLambda({
          compositionId,
          inputProps,
          outputPath,
          renderSettings,
        });
      } else {
        await renderMedia({
          composition,
          serveUrl,
          codec: 'h264',
          outputLocation: outputPath,
          inputProps,
          concurrency: renderSettings.concurrency,
          timeoutInMilliseconds: renderSettings.timeoutInMilliseconds,
        });
      }
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
            ...(isEditorial ? {'katcha-editorial-digest': editorialDigest} : {}),
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
        render_backend: renderSettings.backend,
        ...backendMetadata,
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
        renderer: renderSettings.backend === 'lambda' ? 'remotion-lambda' : 'remotion',
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
  } finally {
    if (isEditorial) activeEditorialOutputs.delete(manifest.output_key);
  }
});

app.listen(port, '0.0.0.0', () => {
  console.log(
    `katcha renderer listening on ${port} `
    + `(backend=${renderSettings.backend}, `
    + `concurrency=${renderSettings.concurrency}, `
    + `frame_timeout_ms=${renderSettings.timeoutInMilliseconds})`,
  );
});
