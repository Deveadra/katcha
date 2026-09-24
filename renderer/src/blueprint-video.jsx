import React from 'react';
import {
  AbsoluteFill,
  Audio,
  OffthreadVideo,
  Sequence,
  useCurrentFrame,
  useVideoConfig,
} from 'remotion';

const secondsToFrames = (seconds, fps) => Math.max(0, Math.round(seconds * fps));

const HeaderPanel = ({header, brand}) => {
  if (!header?.enabled) return null;
  return (
    <div
      style={{
        position: 'absolute',
        top: 0,
        left: 0,
        right: 0,
        height: header.height_px,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        paddingLeft: header.horizontal_padding_px,
        paddingRight: header.horizontal_padding_px,
        boxSizing: 'border-box',
        background: header.background,
        color: header.foreground,
        fontFamily: brand?.captions?.font_family || 'Arial, Helvetica, sans-serif',
        fontSize: header.font_size_px,
        fontWeight: header.font_weight,
        lineHeight: 1.08,
        textAlign: 'center',
        textWrap: 'balance',
      }}
    >
      {header.text}
    </div>
  );
};

const Captions = ({narration, brand}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  if (!narration?.cues?.length) return null;
  const seconds = frame / fps;
  const cue = narration.cues.find(
    (item) => seconds >= item.start_seconds && seconds < item.end_seconds,
  );
  if (!cue) return null;

  const captions = brand?.captions || {};
  const palette = brand?.palette || {};
  return (
    <div
      style={{
        position: 'absolute',
        left: 64,
        right: 64,
        bottom: captions.bottom_safe_zone_px || 250,
        color: palette.paper || '#FFFFFF',
        fontFamily: captions.font_family || 'Arial, Helvetica, sans-serif',
        fontSize: captions.font_size_px || 66,
        fontWeight: captions.font_weight || 900,
        lineHeight: 1.05,
        textAlign: 'center',
        textShadow: `0 4px 13px ${palette.ink || '#000000'}`,
        WebkitTextStroke: `2px ${palette.ink || '#000000'}`,
        textWrap: 'balance',
      }}
    >
      {cue.text}
    </div>
  );
};

export const BlueprintVideo = ({source, header, narration, brand}) => {
  const {fps, height} = useVideoConfig();
  const durationInFrames = Math.max(1, secondsToFrames(source.duration_seconds, fps));
  const sourceHeight = Math.max(1, height - source.top_px);
  const startFrom = secondsToFrames(source.source_start_seconds, fps);

  return (
    <AbsoluteFill style={{backgroundColor: brand?.palette?.ink || '#000000'}}>
      <Sequence durationInFrames={durationInFrames}>
        <div
          style={{
            position: 'absolute',
            top: source.top_px,
            left: 0,
            right: 0,
            height: sourceHeight,
            overflow: 'hidden',
            background: '#000000',
          }}
        >
          {source.background_mode === 'blurred_fill' ? (
            <OffthreadVideo
              src={source.url}
              startFrom={startFrom}
              muted
              style={{
                width: '100%',
                height: '100%',
                objectFit: 'cover',
                filter: 'blur(40px)',
                opacity: 0.42,
                transform: 'scale(1.13)',
              }}
            />
          ) : null}
          <OffthreadVideo
            src={source.url}
            startFrom={startFrom}
            volume={source.audio_volume}
            style={{
              position: 'absolute',
              inset: 0,
              width: '100%',
              height: '100%',
              objectFit: source.fit,
            }}
          />
        </div>
      </Sequence>

      <HeaderPanel header={header} brand={brand} />

      {narration ? (
        <Sequence
          from={secondsToFrames(narration.start_seconds, fps)}
          durationInFrames={Math.max(1, secondsToFrames(narration.duration_seconds, fps))}
        >
          <Audio src={narration.url} volume={1} />
        </Sequence>
      ) : null}

      <Captions narration={narration} brand={brand} />
    </AbsoluteFill>
  );
};
