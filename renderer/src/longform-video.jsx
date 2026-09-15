import React from 'react';
import {
  AbsoluteFill,
  Audio,
  OffthreadVideo,
  Sequence,
  interpolate,
  useCurrentFrame,
  useVideoConfig,
} from 'remotion';

const secondsToFrames = (seconds, fps) => Math.max(0, Math.round(seconds * fps));

const GlobalCaptions = ({timeline}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const seconds = frame / fps;
  const cue = timeline
    .filter((item) => item.narration)
    .flatMap((item) => item.narration.cues || [])
    .find((item) => seconds >= item.start_seconds && seconds < item.end_seconds);

  if (!cue) {
    return null;
  }

  return (
    <div
      style={{
        position: 'absolute',
        left: 180,
        right: 180,
        bottom: 105,
        textAlign: 'center',
        fontFamily: 'Arial, Helvetica, sans-serif',
        fontSize: 54,
        fontWeight: 900,
        lineHeight: 1.08,
        color: 'white',
        textShadow: '0 4px 14px rgba(0,0,0,0.95)',
        WebkitTextStroke: '1.5px rgba(0,0,0,0.72)',
      }}
    >
      {cue.text}
    </div>
  );
};

const ClipItem = ({item}) => {
  const {fps} = useVideoConfig();
  const frame = useCurrentFrame();
  const source = item.clip;
  const sourceStart = secondsToFrames(source.source_start_seconds || 0, fps);
  const localFrame = frame - secondsToFrames(item.start_seconds, fps);
  const transitionFrames = Math.max(1, secondsToFrames(1.8, fps));
  const transitionOpacity = interpolate(
    localFrame,
    [0, Math.min(8, transitionFrames), transitionFrames],
    [0, 1, 0],
    {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'},
  );

  return (
    <AbsoluteFill style={{backgroundColor: '#09090b'}}>
      <OffthreadVideo
        src={source.url}
        startFrom={sourceStart}
        muted
        style={{
          width: '100%',
          height: '100%',
          objectFit: 'cover',
          filter: 'blur(44px) brightness(0.52)',
          opacity: 0.62,
          transform: 'scale(1.12)',
        }}
      />
      <OffthreadVideo
        src={source.url}
        startFrom={sourceStart}
        volume={source.audio_volume}
        style={{width: '100%', height: '100%', objectFit: 'contain'}}
      />
      {source.transition_text ? (
        <div
          style={{
            position: 'absolute',
            left: 70,
            top: 54,
            maxWidth: '72%',
            padding: '14px 22px',
            borderRadius: 16,
            background: 'rgba(8,8,10,0.76)',
            color: 'white',
            fontFamily: 'Arial, Helvetica, sans-serif',
            fontSize: 34,
            fontWeight: 800,
            lineHeight: 1.12,
            opacity: transitionOpacity,
          }}
        >
          {source.transition_text}
        </div>
      ) : null}
    </AbsoluteFill>
  );
};

const NarrationItem = ({item, titleAngle}) => {
  const {fps} = useVideoConfig();
  const narration = item.narration;
  const backgroundStart = secondsToFrames(
    narration.background_source_start_seconds || 0,
    fps,
  );

  return (
    <AbsoluteFill style={{backgroundColor: '#0b0b0f'}}>
      {narration.background_url ? (
        <OffthreadVideo
          src={narration.background_url}
          startFrom={backgroundStart}
          muted
          style={{
            width: '100%',
            height: '100%',
            objectFit: 'cover',
            filter: 'blur(34px) brightness(0.34)',
            transform: 'scale(1.11)',
          }}
        />
      ) : null}
      <AbsoluteFill style={{background: 'rgba(7,7,10,0.48)'}} />
      <AbsoluteFill
        style={{
          justifyContent: 'center',
          alignItems: 'center',
          padding: '120px 220px',
        }}
      >
        <div
          style={{
            color: 'rgba(255,255,255,0.58)',
            fontFamily: 'Arial, Helvetica, sans-serif',
            fontSize: 24,
            fontWeight: 700,
            letterSpacing: 1.8,
            textTransform: 'uppercase',
            marginBottom: 28,
            textAlign: 'center',
          }}
        >
          {titleAngle}
        </div>
        <div
          style={{
            color: 'white',
            fontFamily: 'Arial, Helvetica, sans-serif',
            fontSize: 52,
            fontWeight: 850,
            lineHeight: 1.1,
            textAlign: 'center',
            textShadow: '0 4px 18px rgba(0,0,0,0.9)',
          }}
        >
          {narration.text}
        </div>
      </AbsoluteFill>
      <Audio src={narration.url} volume={1} />
    </AbsoluteFill>
  );
};

export const LongformVideo = ({timeline, title_angle: titleAngle}) => {
  const {fps} = useVideoConfig();

  return (
    <AbsoluteFill style={{backgroundColor: '#09090b'}}>
      {timeline.map((item, index) => {
        const from = secondsToFrames(item.start_seconds, fps);
        const durationInFrames = Math.max(
          1,
          secondsToFrames(item.duration_seconds, fps),
        );
        return (
          <Sequence
            key={`${item.kind}-${item.start_seconds}-${index}`}
            from={from}
            durationInFrames={durationInFrames}
          >
            {item.kind === 'clip' ? (
              <ClipItem item={item} />
            ) : (
              <NarrationItem item={item} titleAngle={titleAngle} />
            )}
          </Sequence>
        );
      })}
      <GlobalCaptions timeline={timeline} />
    </AbsoluteFill>
  );
};
