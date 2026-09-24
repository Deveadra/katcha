import React from 'react';
import {ReactionTrack} from './reaction-track.jsx';
import {
  AbsoluteFill,
  Audio,
  interpolate,
  OffthreadVideo,
  Sequence,
  useCurrentFrame,
  useVideoConfig,
} from 'remotion';

const secondsToFrames = (seconds, fps) => Math.max(0, Math.round(seconds * fps));

const fallbackBrand = {
  palette: {
    ink: '#101216',
    paper: '#F6F3EC',
    signal_blue: '#5B6CFF',
    hot_peach: '#FF7657',
    volt: '#D9FF57',
  },
  captions: {
    font_family: 'Arial, Helvetica, sans-serif',
    font_size_px: 66,
    font_weight: 900,
    bottom_safe_zone_px: 250,
  },
  end_card: {
    accent_role: 'signal_blue',
    label: 'Your ruling',
  },
};

const resolveBrand = (brand) => ({
  ...fallbackBrand,
  ...(brand || {}),
  palette: {...fallbackBrand.palette, ...(brand?.palette || {})},
  captions: {...fallbackBrand.captions, ...(brand?.captions || {})},
  end_card: {...fallbackBrand.end_card, ...(brand?.end_card || {})},
});

const sourceVolume = (source) => {
  if (source.native_audio_policy === 'mute') return 0;
  if (source.native_audio_policy === 'retain') return source.audio_volume;
  return source.narration_duck_volume;
};

const CountdownBadge = ({position, role, brand}) => {
  const palette = brand.palette;
  const accent = role === 'payoff' ? palette.volt : role === 'false_peak' ? palette.hot_peach : palette.signal_blue;
  return (
    <div
      style={{
        position: 'absolute',
        top: 92,
        left: 62,
        minWidth: 150,
        padding: '18px 26px 16px',
        borderRadius: 26,
        background: palette.ink,
        border: `5px solid ${accent}`,
        boxShadow: '0 12px 32px rgba(0,0,0,0.32)',
        color: palette.paper,
        fontFamily: brand.captions.font_family,
        fontSize: 72,
        fontWeight: 950,
        lineHeight: 0.95,
        textAlign: 'center',
      }}
    >
      #{position}
    </div>
  );
};

const ClipScene = ({item, brand}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const fadeFrames = item.transition_before === 'cut' ? 1 : Math.max(2, Math.round(fps * 0.12));
  const opacity = interpolate(frame, [0, fadeFrames], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });
  const scale = item.transition_before === 'punch_cut'
    ? interpolate(frame, [0, fadeFrames], [1.04, 1], {
        extrapolateLeft: 'clamp',
        extrapolateRight: 'clamp',
      })
    : 1;
  const sourceStartFrames = secondsToFrames(item.source.source_start_seconds, fps);

  return (
    <AbsoluteFill style={{opacity, backgroundColor: brand.palette.ink}}>
      {item.transition_before === 'flash' && frame < fadeFrames ? (
        <AbsoluteFill style={{backgroundColor: brand.palette.paper, opacity: 0.42}} />
      ) : null}
      <OffthreadVideo
        src={item.source.url}
        startFrom={sourceStartFrames}
        muted
        style={{
          width: '100%',
          height: '100%',
          objectFit: 'cover',
          filter: 'blur(40px)',
          opacity: 0.4,
          transform: `scale(${1.13 * scale})`,
        }}
      />
      <OffthreadVideo
        src={item.source.url}
        startFrom={sourceStartFrames}
        volume={sourceVolume(item.source)}
        style={{
          width: '100%',
          height: '100%',
          objectFit: 'contain',
          transform: `scale(${scale})`,
        }}
      />
      <CountdownBadge position={item.position} role={item.role} brand={brand} />
    </AbsoluteFill>
  );
};

const Captions = ({overlays, brand}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const seconds = frame / fps;
  const cue = overlays
    .flatMap((overlay) => overlay.cues || [])
    .find((item) => seconds >= item.start_seconds && seconds < item.end_seconds);
  if (!cue) return null;

  return (
    <div
      style={{
        position: 'absolute',
        left: 64,
        right: 64,
        bottom: brand.captions.bottom_safe_zone_px,
        textAlign: 'center',
        fontFamily: brand.captions.font_family,
        fontSize: brand.captions.font_size_px,
        fontWeight: brand.captions.font_weight,
        lineHeight: 1.05,
        color: brand.palette.paper,
        textShadow: `0 4px 13px ${brand.palette.ink}`,
        WebkitTextStroke: `2px ${brand.palette.ink}`,
        textWrap: 'balance',
      }}
    >
      {cue.text}
    </div>
  );
};

const EndCard = ({endCard, brand}) => {
  const {fps} = useVideoConfig();
  if (!endCard?.prompt) return null;
  const from = secondsToFrames(endCard.start_seconds, fps);
  const durationInFrames = Math.max(1, secondsToFrames(endCard.duration_seconds, fps));
  const accent = brand.palette[brand.end_card.accent_role] || brand.palette.signal_blue;

  return (
    <Sequence from={from} durationInFrames={durationInFrames}>
      <AbsoluteFill
        style={{
          justifyContent: 'center',
          alignItems: 'center',
          padding: 78,
          background: brand.palette.ink,
        }}
      >
        <div
          style={{
            width: '100%',
            maxWidth: 900,
            borderTop: `12px solid ${accent}`,
            borderRadius: 28,
            padding: '48px 54px 54px',
            background: 'rgba(246,243,236,0.055)',
          }}
        >
          <div
            style={{
              marginBottom: 20,
              color: accent,
              fontFamily: brand.captions.font_family,
              fontSize: 28,
              fontWeight: 900,
              letterSpacing: 2.2,
              textTransform: 'uppercase',
            }}
          >
            {brand.end_card.label || 'Your ruling'}
          </div>
          <div
            style={{
              color: brand.palette.paper,
              fontFamily: brand.captions.font_family,
              fontSize: 58,
              fontWeight: 850,
              lineHeight: 1.08,
            }}
          >
            {endCard.prompt}
          </div>
        </div>
      </AbsoluteFill>
    </Sequence>
  );
};

export const RankedEpisodeVideo = ({items, overlays, end_card: endCard, brand: brandInput, reaction_events: reactionEvents = []}) => {
  const {fps} = useVideoConfig();
  const brand = resolveBrand(brandInput);

  return (
    <AbsoluteFill style={{backgroundColor: brand.palette.ink}}>
      {items.map((item) => {
        const from = secondsToFrames(item.timeline_start_seconds, fps);
        const durationInFrames = Math.max(
          1,
          secondsToFrames(item.timeline_end_seconds - item.timeline_start_seconds, fps),
        );
        return (
          <Sequence key={`${item.source.clip_id}-${item.position}`} from={from} durationInFrames={durationInFrames}>
            <ClipScene item={item} brand={brand} />
          </Sequence>
        );
      })}

      {overlays.map((overlay) => {
        const from = secondsToFrames(overlay.start_seconds, fps);
        const durationInFrames = Math.max(1, secondsToFrames(overlay.duration_seconds, fps));
        return (
          <Sequence key={`${overlay.asset_key}-${overlay.sequence}`} from={from} durationInFrames={durationInFrames}>
            <Audio src={overlay.url} volume={1} />
          </Sequence>
        );
      })}

      <EndCard endCard={endCard} brand={brand} />
      <ReactionTrack events={reactionEvents} captions={brand.captions} />
      <Captions overlays={overlays} brand={brand} />
    </AbsoluteFill>
  );
};
