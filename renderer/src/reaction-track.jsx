import React from 'react';
import {
  Img,
  interpolate,
  Sequence,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from 'remotion';

// All channel-specific art and timing comes from the frozen production manifest.
const secondsToFrames = (seconds, fps) => Math.max(0, Math.round(seconds * fps));

const ReactionSprite = ({event, captions}) => {
  const frame = useCurrentFrame();
  const {fps, width, height} = useVideoConfig();
  const durationFrames = Math.max(1, secondsToFrames(event.duration_seconds, fps));
  const enterFrames = Math.max(1, Math.round(fps * 0.18));
  const exitFrames = Math.max(1, Math.round(fps * 0.15));
  const enter = interpolate(frame, [0, enterFrames], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });
  const fadeOut = interpolate(
    frame,
    [Math.max(0, durationFrames - exitFrames - 1), durationFrames - 1],
    [1, 0],
    {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'},
  );
  const entranceScale = spring({
    frame,
    fps,
    config: {damping: 12, stiffness: 210, mass: 0.85},
    durationInFrames: Math.max(2, Math.round(fps * 0.3)),
  });
  const animation = event.animation || 'pop_bounce';
  const scale = animation === 'pop_bounce' ? 0.72 + entranceScale * 0.28 : 1;
  const slideOffset = animation === 'slide' ? (1 - enter) * 46 : 0;
  const leftSide = event.anchor?.endsWith('left');
  const topSide = event.anchor?.startsWith('top');
  const marginX = Math.round(width * 0.055);
  // Leave headroom for countdown badges and the YouTube top UI.
  const top = Math.round(height * 0.16);
  // Place bottom-anchored art ABOVE the full caption block, not in the caption band.
  const bottom = (captions?.bottom_safe_zone_px ?? 250)
    + (captions?.font_size_px ?? 66) * (captions?.max_visual_lines ?? 2) * 1.1
    + Math.round(height * 0.025);
  const size = width * (event.scale ?? 0.22);

  return (
    <Img
      src={event.url}
      style={{
        position: 'absolute',
        width: size,
        height: size,
        objectFit: 'contain',
        pointerEvents: 'none',
        ...(leftSide ? {left: marginX} : {right: marginX}),
        ...(topSide ? {top} : {bottom}),
        opacity: Math.min(animation === 'pop_bounce' ? 1 : enter, fadeOut),
        transform: `translateY(${slideOffset}px) scale(${scale})`,
        filter: 'drop-shadow(0 7px 16px rgba(0,0,0,0.4))',
      }}
    />
  );
};

export const ReactionTrack = ({events = [], captions}) => {
  const {fps} = useVideoConfig();
  return (
    <>
      {events.map((event) => (
        <Sequence
          key={event.id}
          from={secondsToFrames(event.start_seconds, fps)}
          durationInFrames={Math.max(1, secondsToFrames(event.duration_seconds, fps))}
          layout="none"
        >
          <ReactionSprite event={event} captions={captions} />
        </Sequence>
      ))}
    </>
  );
};
