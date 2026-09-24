import React from 'react';
import {Composition, registerRoot, staticFile} from 'remotion';
import {BlueprintVideo} from '../src/blueprint-video.jsx';

const brand = {
  palette: {ink: '#101216', paper: '#F6F3EC'},
  captions: {
    font_family: 'Arial, Helvetica, sans-serif',
    font_size_px: 34,
    font_weight: 900,
    bottom_safe_zone_px: 110,
  },
};

const source = {
  url: staticFile('synthetic-source.mp4'),
  duration_seconds: 2.7,
  source_start_seconds: 0,
  source_end_seconds: 2.7,
  top_px: 0,
  fit: 'contain',
  background_mode: 'blurred_fill',
  audio_volume: 0.15,
};

const Persona = () => (
  <BlueprintVideo
    source={source}
    header={{enabled: false, height_px: 0, text: null}}
    narration={{
      url: staticFile('silence.wav'),
      start_seconds: 0.15,
      duration_seconds: 1.3,
      cues: [{start_seconds: 0.15, end_seconds: 1.45, text: 'Persona commentary'}],
    }}
    brand={brand}
  />
);

const Header = () => (
  <BlueprintVideo
    source={{
      ...source,
      top_px: 180,
      background_mode: 'solid',
      audio_volume: 0.72,
    }}
    header={{
      enabled: true,
      height_px: 180,
      text: 'Explanation lives here, not in another channel renderer.',
      background: '#000000',
      foreground: '#FFFFFF',
      font_size_px: 30,
      font_weight: 850,
      horizontal_padding_px: 28,
    }}
    narration={null}
    brand={brand}
  />
);

const Root = () => (
  <>
    <Composition
      id="SyntheticPersonaBlueprint"
      component={Persona}
      durationInFrames={81}
      fps={30}
      width={540}
      height={960}
      defaultProps={{}}
    />
    <Composition
      id="SyntheticHeaderBlueprint"
      component={Header}
      durationInFrames={81}
      fps={30}
      width={540}
      height={960}
      defaultProps={{}}
    />
  </>
);

registerRoot(Root);
