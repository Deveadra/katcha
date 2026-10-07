// Actual synthetic media acceptance: no provider, S3, rights, or publication claims.
import fs from 'node:fs/promises';
import os from 'node:os';
import {createHash} from 'node:crypto';
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
  execFileSync('ffmpeg', ['-y', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=24000:duration=2', '-c:a', 'pcm_s16le', path.join(publicDir, 'narration.wav')], {stdio: 'ignore'});
  execFileSync('ffmpeg', ['-y', '-f', 'lavfi', '-i', 'color=c=navy:size=640x360', '-frames:v', '1', path.join(publicDir, 'still.png')], {stdio: 'ignore'});
  execFileSync('ffmpeg', ['-y', '-f', 'lavfi', '-i', 'color=c=teal:size=360x640', '-frames:v', '1', path.join(publicDir, 'portrait.png')], {stdio: 'ignore'});
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
  const professional = structuredClone(fixture);
  professional.version = 'editorial-render-v5';
  professional.timeline[0].media[0].crop = {x: .2, y: .15, width: .6, height: .7};
  professional.timeline[0].media[0].playback_rate = .75;
  professional.timeline[0].media[0].push_in = 1.08;
  Object.assign(professional.timeline[0], {
    caption_position: 'center',
    caption_scale: 1.15,
    caption_background: true,
    transition: 'fade',
    transition_frames: 6,
  });
  validateEditorialManifest(professional);
  professional.media[0].url = '/public/test.mp4';
  const professionalOutput = path.resolve('test-results/editorial-professional-controls.mp4');
  const professionalComposition = await selectComposition({
    serveUrl,
    id: 'Editorial',
    inputProps: professional,
    ...options,
  });
  await renderMedia({
    serveUrl,
    composition: professionalComposition,
    inputProps: professional,
    codec: 'h264',
    outputLocation: professionalOutput,
    concurrency: 1,
    ...options,
  });
  const professionalProbe = JSON.parse(execFileSync(
    'ffprobe',
    ['-v', 'error', '-show_streams', '-of', 'json', professionalOutput],
  ));
  if (Number(professionalProbe.streams.find(s => s.codec_type === 'video').nb_frames) !== 60) {
    throw new Error('Professional edit-control render verification failed');
  }
  execFileSync('ffmpeg', [
    '-y', '-ss', '1', '-i', professionalOutput, '-frames:v', '1',
    path.resolve('test-results/editorial-professional-controls.png'),
  ], {stdio: 'ignore'});
  const narrated = structuredClone(props);
  narrated.version = 'editorial-render-v2'; narrated.presentation_mode = 'narrated';
  const sha = createHash('sha256').update(await fs.readFile(path.join(publicDir, 'narration.wav'))).digest('hex');
  narrated.narration = narrated.timeline.map(scene => ({narration_id: scene.beat_id, beat_id: scene.beat_id, sha256: sha, text_digest: 'a'.repeat(64), storage_key: `editorial/${narrated.project_id}/narration/${scene.beat_id}/${sha}.wav`, sample_rate: 24000, sample_frames: 48000}));
  delete narrated.media[0].url; validateEditorialManifest(narrated);
  narrated.media[0].url = '/public/test.mp4'; narrated.narration.forEach(audio => { audio.url = '/public/narration.wav'; });
  const voiced = path.resolve('test-results/editorial-narrated.mp4');
  const voicedComposition = await selectComposition({serveUrl, id: 'Editorial', inputProps: narrated, ...options});
  await renderMedia({serveUrl, composition: voicedComposition, inputProps: narrated, codec: 'h264', outputLocation: voiced, concurrency: 1, ...options});
  const voicedProbe = JSON.parse(execFileSync('ffprobe', ['-v', 'error', '-show_streams', '-show_format', '-of', 'json', voiced]));
  if (!voicedProbe.streams.some(stream => stream.codec_type === 'audio') || Number(voicedProbe.streams.find(stream => stream.codec_type === 'video').nb_frames) !== 180) throw new Error('Narrated render has no audio or incorrect frame count');
  const still = structuredClone(fixture);
  const imageSha = createHash('sha256').update(await fs.readFile(path.join(publicDir, 'still.png'))).digest('hex');
  still.version = 'editorial-render-v3'; still.media = [];
  still.images = [{image_id: 'still', beat_id: 'beat', storage_key: `editorial/${still.project_id}/images/still/${imageSha}.png`, sha256: imageSha, width: 640, height: 360, title: 'Synthetic original art', illustration: true}];
  still.timeline[0] = {...still.timeline[0], layout: 'image', media: [], overlays: [], image_id: 'still', image_push_in: 1.1};
  still.presentation_mode = 'narrated'; still.narration = [structuredClone(narrated.narration[0])];
  delete still.narration[0].url; validateEditorialManifest(still);
  still.images[0].url = '/public/still.png'; still.narration[0].url = '/public/narration.wav';
  const stillOutput = path.resolve('test-results/editorial-still.mp4');
  const stillComposition = await selectComposition({serveUrl, id: 'Editorial', inputProps: still, ...options});
  await renderMedia({serveUrl, composition: stillComposition, inputProps: still, codec: 'h264', outputLocation: stillOutput, concurrency: 1, ...options});
  const stillProbe = JSON.parse(execFileSync('ffprobe', ['-v', 'error', '-show_streams', '-of', 'json', stillOutput]));
  if (!stillProbe.streams.some(s => s.codec_type === 'audio') || Number(stillProbe.streams.find(s => s.codec_type === 'video').nb_frames) !== 60) throw new Error('Still render verification failed');
  execFileSync('ffmpeg', ['-y', '-ss', '1', '-i', stillOutput, '-frames:v', '1', path.resolve('test-results/editorial-still.png')], {stdio: 'ignore'});
  const compared = structuredClone(still);
  delete compared.images[0].url;
  compared.narration.forEach(audio => { delete audio.url; });
  compared.version = 'editorial-render-v4';
  const portraitSha = createHash('sha256').update(await fs.readFile(path.join(publicDir, 'portrait.png'))).digest('hex');
  compared.images.push({...compared.images[0], image_id: 'portrait', sha256: portraitSha, width: 360, height: 640, title: 'Synthetic portrait', storage_key: `editorial/${compared.project_id}/images/portrait/${portraitSha}.png`});
  Object.assign(compared.timeline[0], {layout: 'image_comparison', image_id: null, image_ids: ['still', 'portrait'], overlays: [
    {kind: 'highlight', media_index: 0, region: {x: .1, y: .1, width: .8, height: .8}, label: 'Landscape'},
    {kind: 'circle', media_index: 1, region: {x: .1, y: .1, width: .8, height: .8}, label: 'Portrait'},
  ]});
  validateEditorialManifest(compared);
  compared.images[0].url = '/public/still.png'; compared.images[1].url = '/public/portrait.png';
  compared.narration.forEach(audio => { audio.url = '/public/narration.wav'; });
  const compareOutput = path.resolve('test-results/editorial-image-comparison.mp4');
  const compareComposition = await selectComposition({serveUrl, id: 'Editorial', inputProps: compared, ...options});
  await renderMedia({serveUrl, composition: compareComposition, inputProps: compared, codec: 'h264', outputLocation: compareOutput, concurrency: 1, ...options});
  const compareProbe = JSON.parse(execFileSync('ffprobe', ['-v', 'error', '-show_streams', '-of', 'json', compareOutput]));
  if (!compareProbe.streams.some(s => s.codec_type === 'audio') || Number(compareProbe.streams.find(s => s.codec_type === 'video').nb_frames) !== 60) throw new Error('Image comparison render verification failed');
  execFileSync('ffmpeg', ['-y', '-ss', '1', '-i', compareOutput, '-frames:v', '1', path.resolve('test-results/editorial-image-comparison.png')], {stdio: 'ignore'});
  const branded = structuredClone(fixture);
  branded.version = 'editorial-render-v6';
  branded.brand = {
    brand_key: 'forescene',
    version: 4,
    theme_key: 'cinema_v1',
    palette: {
      ink: '#111216',
      paper: '#F7F7F4',
      signal_blue: '#6B7CFF',
      hot_peach: '#FF7657',
      volt: '#D9FF57',
    },
    captions: {
      treatment_key: 'impact_clean_v1',
      font_family: 'Arial, Helvetica, sans-serif',
      font_size_px: 66,
      font_weight: 900,
      max_visual_lines: 2,
      bottom_safe_zone_px: 250,
    },
    motion: {
      treatment_key: 'restrained_punch_v1',
      max_punch_scale: 1.08,
      freeze_frame_max_frames: 8,
      random_motion_enabled: false,
    },
    end_card: {
      treatment_key: 'verdict_v1',
      accent_role: 'signal_blue',
      max_question_lines: 3,
      label: 'FORESCENE',
    },
    logo: {
      enabled: true,
      storage_key: 'brands/channel/logos/approved.png',
      x_percent: 90,
      y_percent: 8,
      width_percent: 10,
      opacity: 0.55,
    },
  };
  validateEditorialManifest(branded);
  branded.media[0].url = '/public/test.mp4';
  branded.brand.logo.url = '/public/still.png';
  const brandedOutput = path.resolve('test-results/editorial-branded.mp4');
  const brandedComposition = await selectComposition({
    serveUrl,
    id: 'Editorial',
    inputProps: branded,
    ...options,
  });
  await renderMedia({
    serveUrl,
    composition: brandedComposition,
    inputProps: branded,
    codec: 'h264',
    outputLocation: brandedOutput,
    concurrency: 1,
    ...options,
  });
  const brandedProbe = JSON.parse(execFileSync(
    'ffprobe',
    ['-v', 'error', '-show_streams', '-of', 'json', brandedOutput],
  ));
  if (Number(brandedProbe.streams.find(s => s.codec_type === 'video').nb_frames) !== 60) {
    throw new Error('Branded Editorial render verification failed');
  }
  execFileSync('ffmpeg', [
    '-y', '-ss', '1', '-i', brandedOutput, '-frames:v', '1',
    path.resolve('test-results/editorial-branded.png'),
  ], {stdio: 'ignore'});
  console.log('Verified branded Editorial render: active palette, caption identity and managed logo');

  console.log('Verified image comparison: landscape/portrait, source regions, narration and credits');
  console.log('Verified still-image render: 60 frames with narration and illustration credit');
  console.log('Verified narrated render: 180 frames with real synthetic PCM audio');
  console.log('Verified professional edit controls: crop, speed, push-in, centered caption background and fade');
  console.log('Verified synthetic editorial render: 1920x1080, 180 frames, silent; single, comparison, freeze and quote scenes');
} finally { await fs.rm(temp, {recursive: true, force: true}); }
