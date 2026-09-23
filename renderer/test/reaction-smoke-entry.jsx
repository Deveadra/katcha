import React from 'react';
import {Composition, registerRoot, staticFile} from 'remotion';
import {RankedEpisodeVideo} from '../src/ranked-episode-video.jsx';
import {ShortVideo} from '../src/short-video.jsx';

// Test-only synthetic assets. No channel artwork or source clips are committed.
const fps = 30;
const width = 540;
const height = 960;
const duration = 2.7;
const sourceUrl = staticFile('synthetic-source.mp4');
const audioUrl = staticFile('silence.wav');
const reactionUrl = staticFile('test-only-reaction.png');

const brand = {
  brand_key: 'synthetic_test_channel',
  version: 1,
  palette: {
    ink: '#101216',
    paper: '#F6F3EC',
    signal_blue: '#5B6CFF',
    hot_peach: '#FF7657',
    volt: '#D9FF57',
  },
  captions: {
    font_family: 'Arial, Helvetica, sans-serif',
    font_size_px: 33,
    font_weight: 900,
    max_visual_lines: 2,
    bottom_safe_zone_px: 125,
  },
  end_card: {accent_role: 'signal_blue'},
};

const reaction = {
  id: 'synthetic-meme-cry',
  asset_key: 'meme_cry',
  brand_key: brand.brand_key,
  pack_key: 'test_only',
  pack_version: 1,
  storage_key: 'brands/synthetic_test_channel/reactions/test_only/v1/meme_cry.png',
  url: reactionUrl,
  start_seconds: 0.65,
  duration_seconds: 1.0,
  anchor: 'bottom_right',
  animation: 'pop_bounce',
  scale: 0.25,
};

const caption = {
  start_seconds: 0.55,
  end_seconds: 1.8,
  text: 'All that ice cream wasted!',
};
const shortOverlay = {
  asset_key: 'synthetic-silence',
  url: audioUrl,
  placement: 'mid',
  text: caption.text,
  start_seconds: 0.55,
  duration_seconds: 1.3,
  cues: [caption],
};
const rankedOverlay = {
  ...shortOverlay,
  sequence: 1,
  placement: 'opening',
};

const FixtureShort = ({showReaction = true}) => (
  <ShortVideo
    source={{url: sourceUrl, duration_seconds: duration, audio_volume: 0}}
    overlays={[shortOverlay]}
    interaction_prompt={null}
    brand={brand}
    reaction_events={showReaction ? [reaction] : []}
  />
);

const items = [3, 2, 1].map((position, index) => ({
  position,
  role: position === 1 ? 'payoff' : position === 3 ? 'opener' : 'build',
  timeline_start_seconds: index * 0.9,
  timeline_end_seconds: (index + 1) * 0.9,
  transition_before: 'cut',
  source: {
    clip_id: `test-${position}`,
    url: sourceUrl,
    duration_seconds: 0.9,
    source_start_seconds: 0,
    native_audio_policy: 'mute',
    audio_volume: 0,
    narration_duck_volume: 0,
  },
}));

const FixtureRanked = ({showReaction = true}) => (
  <RankedEpisodeVideo
    items={items}
    overlays={[rankedOverlay]}
    end_card={{start_seconds: duration, duration_seconds: 0.3, prompt: null}}
    brand={brand}
    reaction_events={showReaction ? [reaction] : []}
  />
);

const Root = () => (
  <>
    <Composition
      id="SyntheticShort"
      component={FixtureShort}
      defaultProps={{showReaction: true}}
      durationInFrames={Math.ceil(duration * fps)}
      fps={fps}
      width={width}
      height={height}
    />
    <Composition
      id="SyntheticRanked"
      component={FixtureRanked}
      defaultProps={{showReaction: true}}
      durationInFrames={Math.ceil(duration * fps)}
      fps={fps}
      width={width}
      height={height}
    />
  </>
);

registerRoot(Root);
