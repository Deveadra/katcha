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

const Captions = ({overlays}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const seconds = frame / fps;
  const cue = overlays
    .flatMap((overlay) => overlay.cues || [])
    .find((item) => seconds >= item.start_seconds && seconds < item.end_seconds);

  if (!cue) {
    return null;
  }

  return (
    <div
      style={{
        position: 'absolute',
        left: 70,
        right: 70,
        bottom: 250,
        textAlign: 'center',
        fontFamily: 'Arial, Helvetica, sans-serif',
        fontSize: 66,
        fontWeight: 900,
        lineHeight: 1.08,
        color: 'white',
        textShadow: '0 4px 10px rgba(0,0,0,0.95)',
        WebkitTextStroke: '2px rgba(0,0,0,0.7)',
      }}
    >
      {cue.text}
    </div>
  );
};

const EndCard = ({sourceDuration, interactionPrompt}) => {
  const {fps} = useVideoConfig();
  if (!interactionPrompt) {
    return null;
  }
  const from = secondsToFrames(sourceDuration, fps);
  return (
    <Sequence from={from}>
      <AbsoluteFill
        style={{
          justifyContent: 'center',
          alignItems: 'center',
          padding: 90,
          background: 'rgba(12,12,15,0.94)',
        }}
      >
        <div
          style={{
            color: 'white',
            fontFamily: 'Arial, Helvetica, sans-serif',
            fontSize: 58,
            fontWeight: 800,
            lineHeight: 1.12,
            textAlign: 'center',
          }}
        >
          {interactionPrompt}
        </div>
      </AbsoluteFill>
    </Sequence>
  );
};

export const ShortVideo = ({source, overlays, interaction_prompt: interactionPrompt}) => {
  const {fps} = useVideoConfig();
  const sourceFrames = secondsToFrames(source.duration_seconds, fps);

  return (
    <AbsoluteFill style={{backgroundColor: '#09090b'}}>
      <Sequence durationInFrames={sourceFrames}>
        <AbsoluteFill>
          <OffthreadVideo
            src={source.url}
            muted
            style={{
              width: '100%',
              height: '100%',
              objectFit: 'cover',
              filter: 'blur(38px)',
              opacity: 0.45,
              transform: 'scale(1.13)',
            }}
          />
          <OffthreadVideo
            src={source.url}
            volume={source.audio_volume}
            style={{width: '100%', height: '100%', objectFit: 'contain'}}
          />
        </AbsoluteFill>
      </Sequence>

      {overlays.map((overlay, index) => {
        const from = secondsToFrames(overlay.start_seconds, fps);
        const durationInFrames = Math.max(
          1,
          secondsToFrames(overlay.duration_seconds, fps),
        );
        return (
          <Sequence key={`${overlay.asset_key}-${index}`} from={from} durationInFrames={durationInFrames}>
            <Audio src={overlay.url} volume={1} />
          </Sequence>
        );
      })}

      <EndCard sourceDuration={source.duration_seconds} interactionPrompt={interactionPrompt} />
      <Captions overlays={overlays} />
    </AbsoluteFill>
  );
};
