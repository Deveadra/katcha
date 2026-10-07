// Validate the renderer boundary before cache lookup, signing, or compute dispatch.
const check = (condition, message) => { if (!condition) throw new Error(`invalid editorial manifest: ${message}`); };
const number = (value, min, max) => Number.isFinite(value) && value >= min && value <= max;
const integer = (value, min, max = 108000) => Number.isInteger(value) && number(value, min, max);
const text = (value, max) => typeof value === 'string' && value.length > 0 && value.length <= max;
const key = (value) => text(value, 1000) && !/[:\\\x00-\x1f]/.test(value) && !value.startsWith('/') && !value.split('/').some(part => part === '..' || part === '.');

export const validateEditorialManifest = (manifest) => {
  check(['editorial-render-v1', 'editorial-render-v2', 'editorial-render-v3', 'editorial-render-v4', 'editorial-render-v5'].includes(manifest?.version), 'version');
  const narrated = manifest.presentation_mode === 'narrated';
  check(['editorial-render-v3', 'editorial-render-v4', 'editorial-render-v5'].includes(manifest.version) || narrated === (manifest.version === 'editorial-render-v2'), 'version presentation');
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
  const images = new Map();
  const imageVersion = ['editorial-render-v3', 'editorial-render-v4'].includes(manifest.version);
  const flexibleImageVersion = manifest.version === 'editorial-render-v5';
  check(
    imageVersion
      ? Array.isArray(manifest.images) && manifest.images.length > 0 && manifest.images.length <= 100
      : flexibleImageVersion
        ? manifest.images == null || (Array.isArray(manifest.images) && manifest.images.length <= 100)
        : !manifest.images?.length,
    'image version',
  );
  for (const image of manifest.images || []) {
    check(text(image.image_id, 100) && !images.has(image.image_id) && text(image.beat_id, 120), 'image identity');
    check(/^[a-f0-9]{64}$/.test(image.sha256) && key(image.storage_key) && image.storage_key === `editorial/${manifest.project_id}/images/${image.image_id}/${image.sha256}.png` && !('url' in image), 'managed image');
    check(integer(image.width, 1, 8192) && integer(image.height, 1, 8192) && image.width * image.height <= 16000000, 'image dimensions');
    check(text(image.title, 200) && typeof image.illustration === 'boolean', 'image attribution');
    images.set(image.image_id, image);
  }
  const usedImages = new Set();
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
    check(['single', 'comparison', 'quote', 'image', 'image_comparison'].includes(scene.layout), 'layout');
    check(Array.isArray(scene.media) && scene.media.length === {single: 1, comparison: 2, quote: 0, image: 0, image_comparison: 0}[scene.layout], 'layout media');
    if (scene.layout === 'quote') check(text(scene.quote_source_id, 120) && text(scene.quote_text, 300) && text(scene.source_credit, 200), 'quote provenance');
    const imageLayout = ['image', 'image_comparison'].includes(scene.layout);
    const imageIds = scene.layout === 'image' ? [scene.image_id] : (scene.image_ids || []);
    if (scene.layout === 'image_comparison') {
      check(['editorial-render-v4', 'editorial-render-v5'].includes(manifest.version) && Array.isArray(scene.image_ids) && imageIds.length === 2 && new Set(imageIds).size === 2 && !scene.image_id, 'image comparison');
    } else check(!scene.image_ids?.length, 'unexpected image comparison');
    if (imageLayout) {
      check(number(scene.image_push_in, 1, 1.15), 'image motion');
      for (const id of imageIds) {
        const image = images.get(id);
        check(image && image.beat_id === scene.beat_id, 'image selection');
        usedImages.add(id);
      }
    } else check(!scene.image_id && (scene.image_push_in == null || scene.image_push_in === 1), 'image layout');
    for (const use of scene.media) {
      const asset = assets.get(use.candidate_id);
      check(asset && number(use.start_seconds, 0, asset.duration_seconds) && use.start_seconds < asset.duration_seconds, 'source start');
      check(number(use.playback_rate, 0.25, 2) && typeof use.freeze === 'boolean' && number(use.push_in, 1, 1.15), 'playback');
      if (use.crop != null) {
        const crop = use.crop;
        check(crop && number(crop.x, 0, 1) && number(crop.y, 0, 1) && number(crop.width, Number.MIN_VALUE, 1) && number(crop.height, Number.MIN_VALUE, 1) && crop.x + crop.width <= 1 && crop.y + crop.height <= 1, 'crop region');
      }
      check(use.start_seconds + (use.freeze ? 0 : scene.duration_frames / 30 * use.playback_rate) <= asset.duration_seconds + 1e-6, 'source end');
    }
    check(Array.isArray(scene.overlays) && scene.overlays.length <= 8, 'annotations');
    if (imageLayout && scene.overlays.length) check(['editorial-render-v4', 'editorial-render-v5'].includes(manifest.version), 'image annotation version');
    for (const overlay of scene.overlays) {
      check(['circle', 'arrow', 'highlight'].includes(overlay.kind) && integer(overlay.media_index, 0, (imageLayout ? imageIds.length : scene.media.length) - 1), 'annotation target');
      const r = overlay.region;
      check(r && number(r.x, 0, 1) && number(r.y, 0, 1) && number(r.width, Number.MIN_VALUE, 1) && number(r.height, Number.MIN_VALUE, 1) && r.x + r.width <= 1 && r.y + r.height <= 1, 'annotation region');
      check(overlay.label == null || text(overlay.label, 100), 'annotation label');
    }
    check(scene.uncertainty_disclosure == null || text(scene.uncertainty_disclosure, 180), 'disclosure');
    check(scene.caption_position == null || ['bottom', 'center'].includes(scene.caption_position), 'caption position');
    check(scene.caption_scale == null || number(scene.caption_scale, 0.75, 1.35), 'caption scale');
    check(scene.caption_background == null || typeof scene.caption_background === 'boolean', 'caption background');
    check(scene.transition == null || ['cut', 'fade'].includes(scene.transition), 'transition');
    if (scene.transition === 'fade') check(integer(scene.transition_frames, 3, 15), 'transition frames');
    else check(scene.transition_frames == null, 'cut transition frames');
    check(Array.isArray(scene.captions) && scene.captions.length > 0 && scene.captions.length <= 100, 'captions');
    let captionCursor = 0;
    for (const caption of scene.captions) {
      check(caption.start_frame === captionCursor && integer(caption.duration_frames, 1) && text(caption.text, 300), 'caption coverage');
      captionCursor += caption.duration_frames;
    }
    check(captionCursor === scene.duration_frames, 'caption end');
    cursor += scene.duration_frames;
  }
  const advancedEdits = manifest.timeline.some(scene =>
    scene.caption_position != null
      || scene.caption_scale != null
      || scene.caption_background === true
      || scene.transition != null
      || scene.media.some(use => use.crop != null)
  );
  check(!advancedEdits || manifest.version === 'editorial-render-v5', 'professional edit version');
  check(usedImages.size === images.size, 'image coverage');
  check(!narrated || (narration.size === beats.size && manifest.narration.every((audio, index) => audio.beat_id === manifest.timeline[index]?.beat_id)), 'narration coverage');
  check(cursor <= 108000 && Math.abs(cursor / 30 - manifest.output_duration_seconds) < 1e-6, 'duration');
  return manifest;
};

// Shared geometry keeps annotations in source coordinates through contain and push-in.
export const containRect = (width, height, boxWidth, boxHeight) => {
  const scale = Math.min(boxWidth / width, boxHeight / height);
  return {width: width * scale, height: height * scale, left: (boxWidth - width * scale) / 2, top: (boxHeight - height * scale) / 2};
};


export const sourceViewport = (width, height, boxWidth, boxHeight, crop = null) => {
  if (!crop) {
    return {
      viewport: containRect(width, height, boxWidth, boxHeight),
      content: {left: 0, top: 0, width: '100%', height: '100%'},
    };
  }
  const cropWidth = width * crop.width;
  const cropHeight = height * crop.height;
  const scale = Math.min(boxWidth / cropWidth, boxHeight / cropHeight);
  const viewport = {
    width: cropWidth * scale,
    height: cropHeight * scale,
    left: (boxWidth - cropWidth * scale) / 2,
    top: (boxHeight - cropHeight * scale) / 2,
  };
  return {
    viewport,
    content: {
      left: -width * crop.x * scale,
      top: -height * crop.y * scale,
      width: width * scale,
      height: height * scale,
    },
  };
};
