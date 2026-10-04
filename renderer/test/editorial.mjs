import assert from 'node:assert/strict';
import {validateEditorialManifest, containRect} from '../src/editorial-contract.mjs';

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
