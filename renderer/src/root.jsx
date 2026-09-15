import React from 'react';
import {Composition} from 'remotion';
import {ShortVideo} from './short-video.jsx';

const defaults = {
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

export const RemotionRoot = () => (
  <Composition
    id="Short"
    component={ShortVideo}
    durationInFrames={30}
    fps={30}
    width={1080}
    height={1920}
    defaultProps={defaults}
    calculateMetadata={({props}) => ({
      durationInFrames: Math.max(1, Math.ceil(props.output_duration_seconds * props.fps)),
      fps: props.fps,
      width: props.width,
      height: props.height,
    })}
  />
);
