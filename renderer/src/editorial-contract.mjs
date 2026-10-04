// Validate the renderer boundary before cache lookup, signing, or compute dispatch.
const check = (condition, message) => { if (!condition) throw new Error(`invalid editorial manifest: ${message}`); };
const number = (value, min, max) => Number.isFinite(value) && value >= min && value <= max;
const integer = (value, min, max = 108000) => Number.isInteger(value) && number(value, min, max);
const text = (value, max) => typeof value === 'string' && value.length > 0 && value.length <= max;
const key = (value) => text(value, 1000) && !/[:\\\x00-\x1f]/.test(value) && !value.startsWith('/') && !value.split('/').some(part => part === '..' || part === '.');

export const validateEditorialManifest = (manifest) => {
  check(['editorial-render-v1', 'editorial-render-v2'].includes(manifest?.version), 'version');
  const narrated = manifest.version === 'editorial-render-v2';
  check(manifest.presentation_mode === (narrated ? 'narrated' : 'captioned_silent') && manifest.requires_editorial_review === true, 'presentation and review');
  check(narrated ? Array.isArray(manifest.narration) && manifest.narration.length > 0 && manifest.narration.length <= 100 : !manifest.narration?.length, 'narration mode');
  const narration = new Map();
  for (const audio of manifest.narration || []) {
    check(text(audio.beat_id, 120) && !narration.has(audio.beat_id) && text(audio.narration_id, 100), 'narration identity');
    check(/^[a-f0-9]{64}$/.test(audio.sha256) && /^[a-f0-9]{64}$/.test(audio.text_digest), 'narration lineage');
    check(key(audio.storage_key) && audio.storage_key === `editorial/${manifest.project_id}/narration/${audio.narration_id}/${audio.sha256}.wav` && !('url' in audio), 'managed narration');
    check(integer(audio.sample_rate, 8000, 96000) && integer(audio.sample_frames, 1, audio.sample_rate * 600), 'measured narration');
    narration.set(audio.beat_id, audio);
  }
  check(manifest.width === 1920 && manifest.height === 1080 && manifest.fps === 30, 'dimensions');
  check(text(manifest.project_id, 100) && integer(manifest.revision, 1, Number.MAX_SAFE_INTEGER), 'lineage');
  check(/^[a-f0-9]{64}$/.test(manifest.draft_digest), 'draft digest');
  check(key(manifest.output_key) && manifest.output_key.startsWith(`editorial/${manifest.project_id}/${manifest.revision}/`) && /\/[a-f0-9]{64}\.mp4$/.test(manifest.output_key), 'output key');
  check(Array.isArray(manifest.media) && manifest.media.length <= 30, 'media');
  const assets = new Map();
  for (const asset of manifest.media) {
    check(text(asset.candidate_id, 120) && !assets.has(asset.candidate_id), 'media identity');
    check(key(asset.storage_key) && !('url' in asset), 'managed media');
    check(/^[a-f0-9]{64}$/.test(asset.sha256) && text(asset.clip_id, 100) && text(asset.rights_assessment_id, 100), 'media lineage');
    check(integer(asset.width, 1, 16384) && integer(asset.height, 1, 16384) && number(asset.duration_seconds, Number.MIN_VALUE, Number.MAX_VALUE), 'measured media');
    assets.set(asset.candidate_id, asset);
  }
  check(Array.isArray(manifest.timeline) && manifest.timeline.length > 0 && manifest.timeline.length <= 100, 'timeline');
  let cursor = 0;
  const beats = new Set();
  for (const scene of manifest.timeline) {
    check(text(scene.beat_id, 120) && !beats.has(scene.beat_id), 'beat identity');
    beats.add(scene.beat_id);
    check(scene.start_frame === cursor && integer(scene.duration_frames, 1), 'frame coverage');
    if (narrated) {
      const audio = narration.get(scene.beat_id);
      check(audio && scene.duration_frames === Math.ceil(audio.sample_frames * 30 / audio.sample_rate), 'narration timing');
    }
    check(['single', 'comparison', 'quote'].includes(scene.layout), 'layout');
    check(Array.isArray(scene.media) && scene.media.length === {single: 1, comparison: 2, quote: 0}[scene.layout], 'layout media');
    if (scene.layout === 'quote') check(text(scene.quote_source_id, 120) && text(scene.quote_text, 300) && text(scene.source_credit, 200), 'quote provenance');
    for (const use of scene.media) {
      const asset = assets.get(use.candidate_id);
      check(asset && number(use.start_seconds, 0, asset.duration_seconds) && use.start_seconds < asset.duration_seconds, 'source start');
      check(number(use.playback_rate, 0.25, 2) && typeof use.freeze === 'boolean' && number(use.push_in, 1, 1.15), 'playback');
      check(use.start_seconds + (use.freeze ? 0 : scene.duration_frames / 30 * use.playback_rate) <= asset.duration_seconds + 1e-6, 'source end');
    }
    check(Array.isArray(scene.overlays) && scene.overlays.length <= 8, 'annotations');
    for (const overlay of scene.overlays) {
      check(['circle', 'arrow', 'highlight'].includes(overlay.kind) && integer(overlay.media_index, 0, scene.media.length - 1), 'annotation target');
      const r = overlay.region;
      check(r && number(r.x, 0, 1) && number(r.y, 0, 1) && number(r.width, Number.MIN_VALUE, 1) && number(r.height, Number.MIN_VALUE, 1) && r.x + r.width <= 1 && r.y + r.height <= 1, 'annotation region');
      check(overlay.label == null || text(overlay.label, 100), 'annotation label');
    }
    check(scene.uncertainty_disclosure == null || text(scene.uncertainty_disclosure, 180), 'disclosure');
    check(Array.isArray(scene.captions) && scene.captions.length > 0 && scene.captions.length <= 100, 'captions');
    let captionCursor = 0;
    for (const caption of scene.captions) {
      check(caption.start_frame === captionCursor && integer(caption.duration_frames, 1) && text(caption.text, 300), 'caption coverage');
      captionCursor += caption.duration_frames;
    }
    check(captionCursor === scene.duration_frames, 'caption end');
    cursor += scene.duration_frames;
  }
  check(!narrated || (narration.size === beats.size && manifest.narration.every((audio, index) => audio.beat_id === manifest.timeline[index]?.beat_id)), 'narration coverage');
  check(cursor <= 108000 && Math.abs(cursor / 30 - manifest.output_duration_seconds) < 1e-6, 'duration');
  return manifest;
};

// Shared geometry keeps annotations in source coordinates through contain and push-in.
export const containRect = (width, height, boxWidth, boxHeight) => {
  const scale = Math.min(boxWidth / width, boxHeight / height);
  return {width: width * scale, height: height * scale, left: (boxWidth - width * scale) / 2, top: (boxHeight - height * scale) / 2};
};
