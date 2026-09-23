import React from 'react';
import {ReactionTrack} from './reaction-track.jsx';
import {
  AbsoluteFill,
  Audio,
  OffthreadVideo,
  Sequence,
  useCurrentFrame,
  useVideoConfig,
} from 'remotion';

const secondsToFrames = (seconds, fps) => Math.max(0, Math.round(seconds * fps));

const fallbackBrand = {
  brand_key: 'channel_01',
  version: 1,
  theme_key: 'signal_v1',
  palette: {
    ink: '#101216',
    paper: '#F6F3EC',
    signal_blue: '#5B6CFF',
    hot_peach: '#FF7657',
    volt: '#D9FF57',
  },
  captions: {
    treatment_key: 'impact_clean_v1',
    font_family: 'Arial, Helvetica, sans-serif',
    font_size_px: 66,
    font_weight: 900,
    max_visual_lines: 2,
    bottom_safe_zone_px: 250,
  },
  motion: {
    treatment_key: 'restrained_punch_v1',
    max_punch_scale: 1.08,
    freeze_frame_max_frames: 8,
    random_motion_enabled: false,
  },
  end_card: {
    treatment_key: 'verdict_v1',
    accent_role: 'signal_blue',
    max_question_lines: 3,
  },
};

const resolveBrand = (brand) => ({
  ...fallbackBrand,
  ...(brand || {}),
  palette: {...fallbackBrand.palette, ...(brand?.palette || {})},
  captions: {...fallbackBrand.captions, ...(brand?.captions || {})},
  motion: {...fallbackBrand.motion, ...(brand?.motion || {})},
  end_card: {...fallbackBrand.end_card, ...(brand?.end_card || {})},
});

const Captions = ({overlays, brand}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const seconds = frame / fps;
  const cue = overlays
    .flatMap((overlay) => overlay.cues || [])
    .find((item) => seconds >= item.start_seconds && seconds < item.end_seconds);

  if (!cue) {
    return null;
  }

  const captionStyle = brand.captions;
  const palette = brand.palette;

  return (
    <div
      style={{
        position: 'absolute',
        left: 70,
        right: 70,
        bottom: captionStyle.bottom_safe_zone_px,
        textAlign: 'center',
        fontFamily: captionStyle.font_family,
        fontSize: captionStyle.font_size_px,
        fontWeight: captionStyle.font_weight,
        lineHeight: 1.06,
        color: palette.paper,
        textShadow: `0 4px 12px ${palette.ink}`,
        WebkitTextStroke: `2px ${palette.ink}`,
        textWrap: 'balance',
      }}
    >
      {cue.text}
    </div>
  );
};

const EndCard = ({sourceDuration, interactionPrompt, brand}) => {
  const {fps} = useVideoConfig();
  if (!interactionPrompt) {
    return null;
  }
  const from = secondsToFrames(sourceDuration, fps);
  const palette = brand.palette;
  const accent = palette[brand.end_card.accent_role] || palette.signal_blue;

  return (
    <Sequence from={from}>
      <AbsoluteFill
        style={{
          justifyContent: 'center',
          alignItems: 'center',
          padding: 80,
          background: palette.ink,
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
            boxShadow: '0 18px 60px rgba(0,0,0,0.24)',
          }}
        >
          <div
            style={{
              display: 'inline-block',
              marginBottom: 22,
              color: accent,
              fontFamily: brand.captions.font_family,
              fontSize: 26,
              fontWeight: 900,
              letterSpacing: 2.2,
              textTransform: 'uppercase',
            }}
          >
            Your ruling?
          </div>
          <div
            style={{
              color: palette.paper,
              fontFamily: brand.captions.font_family,
              fontSize: 58,
              fontWeight: 850,
              lineHeight: 1.08,
              textAlign: 'left',
            }}
          >
            {interactionPrompt}
          </div>
        </div>
      </AbsoluteFill>
    </Sequence>
  );
};

export const ShortVideo = ({
  source,
  overlays,
  interaction_prompt: interactionPrompt,
  brand: brandInput,
  reaction_events: reactionEvents = [],
}) => {
  const {fps} = useVideoConfig();
  const sourceFrames = secondsToFrames(source.duration_seconds, fps);
  const brand = resolveBrand(brandInput);

  return (
    <AbsoluteFill style={{backgroundColor: brand.palette.ink}}>
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
              opacity: 0.42,
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

      <EndCard
        sourceDuration={source.duration_seconds}
        interactionPrompt={interactionPrompt}
        brand={brand}
      />
      <ReactionTrack events={reactionEvents} captions={brand.captions} />
      <Captions overlays={overlays} brand={brand} />
    </AbsoluteFill>
  );
};
