import assert from 'node:assert/strict';
import {
  EDITORIAL_MANIFEST_VERSIONS,
  containRect,
  isEditorialManifestVersion,
  sourceViewport,
  validateEditorialManifest,
} from '../src/editorial-contract.mjs';

export const fixture = {
  version: 'editorial-render-v1', project_id: 'test-project', revision: 1,
  draft_digest: 'a'.repeat(64), presentation_mode: 'captioned_silent',
  width: 1920, height: 1080, fps: 30, requires_editorial_review: true,
  output_key: `editorial/test-project/1/${'b'.repeat(64)}.mp4`, output_duration_seconds: 2,
  media: [{candidate_id: 'asset', clip_id: 'clip', storage_key: 'raw/test.mp4', sha256: 'c'.repeat(64), width: 1920, height: 1080, duration_seconds: 4, rights_assessment_id: 'rights'}],
  timeline: [{beat_id: 'beat', layout: 'single', start_frame: 0, duration_frames: 60,
    media: [{candidate_id: 'asset', start_seconds: 1, playback_rate: 1, freeze: false, push_in: 1.1}],
    overlays: [{kind: 'circle', media_index: 0, region: {x: 0.2, y: 0.2, width: 0.3, height: 0.3}, label: 'Measured region'}],
    captions: [{start_frame: 0, duration_frames: 30, text: 'Synthetic renderer test.'}, {start_frame: 30, duration_frames: 30, text: 'The next caption.'}],
    uncertainty_disclosure: 'Unconfirmed theory', quote_source_id: null, quote_text: null, source_credit: null}],
};
assert.equal(validateEditorialManifest(fixture), fixture);
assert.deepEqual(EDITORIAL_MANIFEST_VERSIONS, [
  'editorial-render-v1',
  'editorial-render-v2',
  'editorial-render-v3',
  'editorial-render-v4',
  'editorial-render-v5',
]);
assert.equal(isEditorialManifestVersion('editorial-render-v5'), true);
assert.equal(isEditorialManifestVersion('editorial-render-v6'), false);
for (const mutate of [
  m => {m.media[0].url = 'https://example.com/unchecked.mp4';},
  m => {m.media[0].storage_key = '../private';},
  m => {m.output_key = 'raw/overwrite.mp4';},
  m => {m.timeline[0].media[0].start_seconds = 3;},
  m => {m.timeline[0].captions[1].start_frame = 29;},
  m => {m.timeline[0].overlays[0].media_index = 1;},
  m => {m.timeline[0].overlays[0].region.width = 0.9;},
  m => {m.timeline[0].media[0].push_in = Infinity;},
  m => {m.requires_editorial_review = false;},
  m => {m.output_duration_seconds = 10;},
]) {
  const value = structuredClone(fixture); mutate(value);
  assert.throws(() => validateEditorialManifest(value), /invalid editorial manifest/);
}
const frozen = structuredClone(fixture);
frozen.timeline[0].media[0].start_seconds = 3.9;
frozen.timeline[0].media[0].freeze = true;
validateEditorialManifest(frozen);
assert.deepEqual(containRect(1920, 1080, 912, 744), {width: 912, height: 513, left: 0, top: 115.5});
assert.deepEqual(containRect(1080, 1920, 912, 744), {width: 418.5, height: 744, left: 246.75, top: 0});
console.log('Editorial manifest and geometry checks passed');
const professional = structuredClone(fixture);
professional.version = 'editorial-render-v5';
Object.assign(professional.timeline[0], {
  caption_position: 'center',
  caption_scale: 1.15,
  caption_background: true,
  transition: 'fade',
  transition_frames: 6,
});
professional.timeline[0].media[0].crop = {
  x: 0.25,
  y: 0.25,
  width: 0.5,
  height: 0.5,
};
validateEditorialManifest(professional);
assert.deepEqual(
  sourceViewport(1920, 1080, 912, 744, professional.timeline[0].media[0].crop),
  {
    viewport: {width: 912, height: 513, left: 0, top: 115.5},
    content: {left: -456, top: -256.5, width: 1824, height: 1026},
  },
);
for (const mutate of [
  m => {m.version = 'editorial-render-v1';},
  m => {m.timeline[0].caption_scale = 2;},
  m => {m.timeline[0].transition_frames = 2;},
  m => {m.timeline[0].media[0].crop.width = 0.9;},
]) {
  const value = structuredClone(professional); mutate(value);
  assert.throws(() => validateEditorialManifest(value), /invalid editorial manifest/);
}
console.log('Professional crop, caption and transition manifest checks passed');


const narrated = structuredClone(fixture);
narrated.version = 'editorial-render-v2';
narrated.presentation_mode = 'narrated';
narrated.narration = [{narration_id: 'audio', beat_id: 'beat', sha256: 'd'.repeat(64), text_digest: 'e'.repeat(64), storage_key: `editorial/test-project/narration/audio/${'d'.repeat(64)}.wav`, sample_rate: 24000, sample_frames: 48000}];
validateEditorialManifest(narrated);
for (const mutate of [
  m => {m.narration = [];},
  m => {m.narration[0].sample_frames += 1;},
  m => {m.narration[0].beat_id = 'invented';},
  m => {m.narration[0].url = 'https://example.com/audio';},
  m => {m.narration[0].storage_key = 'other/audio.wav';},
  m => {m.version = 'editorial-render-v1';},
]) {
  const value = structuredClone(narrated); mutate(value);
  assert.throws(() => validateEditorialManifest(value), /invalid editorial manifest/);
}
const {verifyNarrationBytes} = await import('../src/editorial-media.mjs');
const {createHash} = await import('node:crypto');
const {Readable} = await import('node:stream');
const bytes = Buffer.from('test audio content');
await verifyNarrationBytes(Readable.from([bytes]), createHash('sha256').update(bytes).digest('hex'));
await assert.rejects(() => verifyNarrationBytes(Readable.from([bytes]), '0'.repeat(64)), /checksum/);
await assert.rejects(() => verifyNarrationBytes(Readable.from([Buffer.alloc(32 * 1024 * 1024 + 1)]), '0'.repeat(64)), /32 MiB/);
console.log('Narrated manifests and audio checksum bounds passed');

const still = structuredClone(fixture);
still.version = 'editorial-render-v3'; still.media = []; still.narration = [];
still.images = [{image_id: 'still', beat_id: 'beat', storage_key: `editorial/test-project/images/still/${'f'.repeat(64)}.png`, sha256: 'f'.repeat(64), width: 320, height: 180, title: 'Original illustration', illustration: true}];
still.timeline[0] = {...still.timeline[0], layout: 'image', media: [], overlays: [], image_id: 'still', image_push_in: 1.1};
validateEditorialManifest(still);
const voicedStill = structuredClone(still); voicedStill.presentation_mode = 'narrated'; voicedStill.narration = narrated.narration;
validateEditorialManifest(voicedStill);
for (const mutate of [
  m => { m.version = 'editorial-render-v1'; },
  m => { m.images[0].url = 'https://untrusted.example/image'; },
  m => { m.images[0].storage_key = 'raw/unapproved.png'; },
  m => { m.images[0].beat_id = 'other'; },
  m => { m.images[0].width = 8193; },
  m => { m.timeline[0].image_push_in = 2; },
  m => { m.timeline[0].image_id = 'missing'; },
  m => { m.images.push({...m.images[0], image_id: 'unused'}); },
]) {
  const value = structuredClone(still); mutate(value);
  assert.throws(() => validateEditorialManifest(value), /invalid editorial manifest/);
}
const {verifyImageBytes} = await import('../src/editorial-media.mjs');
const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg==', 'base64');
const image = {sha256: createHash('sha256').update(png).digest('hex'), width: 1, height: 1};
await verifyImageBytes(Readable.from([png]), image);
await assert.rejects(() => verifyImageBytes(Readable.from([png]), {...image, sha256: '0'.repeat(64)}), /checksum/);
await assert.rejects(() => verifyImageBytes(Readable.from([png]), {...image, width: 2}), /dimensions/);
await assert.rejects(() => verifyImageBytes(Readable.from([Buffer.alloc(16 * 1024 * 1024 + 1)]), image), /16 MiB/);
console.log('Still-image manifests, narration, PNG identity and byte limits passed');

const imageComparison = structuredClone(still);
imageComparison.version = 'editorial-render-v4';
imageComparison.images.push({...imageComparison.images[0], image_id: 'second', width: 180, height: 320, storage_key: `editorial/test-project/images/second/${'f'.repeat(64)}.png`});
Object.assign(imageComparison.timeline[0], {layout: 'image_comparison', image_id: null, image_ids: ['still', 'second'], overlays: [{kind: 'circle', media_index: 1, region: {x: .1, y: .1, width: .8, height: .8}}]});
validateEditorialManifest(imageComparison);
for (const mutate of [
  m => { m.version = 'editorial-render-v3'; },
  m => { m.timeline[0].image_ids = ['still', 'still']; },
  m => { m.timeline[0].image_ids[1] = 'absent'; },
  m => { m.timeline[0].overlays[0].media_index = 2; },
  m => { m.timeline[0].overlays[0].region.x = .9; },
  m => { m.images[1].beat_id = 'other'; },
]) {
  const value = structuredClone(imageComparison); mutate(value);
  assert.throws(() => validateEditorialManifest(value), /invalid editorial manifest/);
}
const annotatedStill = structuredClone(still);
annotatedStill.version = 'editorial-render-v4';
annotatedStill.timeline[0].overlays = [{kind: 'highlight', media_index: 0, region: {x: 0, y: 0, width: 1, height: 1}}];
validateEditorialManifest(annotatedStill);
annotatedStill.version = 'editorial-render-v3';
assert.throws(() => validateEditorialManifest(annotatedStill), /image annotation version/);
console.log('Versioned image comparison and source-region contracts passed');
