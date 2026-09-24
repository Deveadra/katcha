import React from 'react';
import {Composition} from 'remotion';
import {BlueprintVideo} from './blueprint-video.jsx';
import {LongformVideo} from './longform-video.jsx';
import {RankedEpisodeVideo} from './ranked-episode-video.jsx';
import {ShortVideo} from './short-video.jsx';

const blueprintDefaults = {
  width: 1080,
  height: 1920,
  fps: 30,
  output_duration_seconds: 1,
  source: {
    url: '',
    duration_seconds: 1,
    source_start_seconds: 0,
    top_px: 0,
    fit: 'contain',
    background_mode: 'solid',
    audio_volume: 0,
  },
  header: {enabled: false, height_px: 0, text: null},
  narration: null,
  brand: null,
};

const shortDefaults = {
  width: 1080,
  height: 1920,
  fps: 30,
  output_duration_seconds: 1,
  source: {
    url: '',
    duration_seconds: 1,
    audio_volume: 0.45,
  },
  overlays: [],
  interaction_prompt: null,
};

const rankedEpisodeDefaults = {
  width: 1080,
  height: 1920,
  fps: 30,
  output_duration_seconds: 1,
  items: [],
  overlays: [],
  end_card: {start_seconds: 0, duration_seconds: 1, prompt: null},
};

const longformDefaults = {
  width: 1920,
  height: 1080,
  fps: 30,
  output_duration_seconds: 1,
  title_angle: 'Katcha Compilation',
  timeline: [],
};

export const RemotionRoot = () => (
  <>
    <Composition
      id="BlueprintVideo"
      component={BlueprintVideo}
      durationInFrames={30}
      fps={30}
      width={1080}
      height={1920}
      defaultProps={blueprintDefaults}
      calculateMetadata={({props}) => ({
        durationInFrames: Math.max(1, Math.ceil(props.output_duration_seconds * props.fps)),
        fps: props.fps,
        width: props.width,
        height: props.height,
      })}
    />
    <Composition
      id="Short"
      component={ShortVideo}
      durationInFrames={30}
      fps={30}
      width={1080}
      height={1920}
      defaultProps={shortDefaults}
      calculateMetadata={({props}) => ({
        durationInFrames: Math.max(1, Math.ceil(props.output_duration_seconds * props.fps)),
        fps: props.fps,
        width: props.width,
        height: props.height,
      })}
    />
    <Composition
      id="RankedEpisode"
      component={RankedEpisodeVideo}
      durationInFrames={30}
      fps={30}
      width={1080}
      height={1920}
      defaultProps={rankedEpisodeDefaults}
      calculateMetadata={({props}) => ({
        durationInFrames: Math.max(1, Math.ceil(props.output_duration_seconds * props.fps)),
        fps: props.fps,
        width: props.width,
        height: props.height,
      })}
    />
    <Composition
      id="Longform"
      component={LongformVideo}
      durationInFrames={30}
      fps={30}
      width={1920}
      height={1080}
      defaultProps={longformDefaults}
      calculateMetadata={({props}) => ({
        durationInFrames: Math.max(1, Math.ceil(props.output_duration_seconds * props.fps)),
        fps: props.fps,
        width: props.width,
        height: props.height,
      })}
    />
  </>
);
