import React from 'react';
import {AbsoluteFill, Img} from 'remotion';

const textPosition = (position) => {
  if (position === 'top') return {top: 48};
  if (position === 'center') return {top: '50%', transform: 'translateY(-50%)'};
  return {bottom: 48};
};

export const Thumbnail = ({source, text, brand}) => {
  const palette = brand?.palette || {};
  const captions = brand?.captions || {};
  return (
    <AbsoluteFill style={{backgroundColor: palette.ink || '#101216'}}>
      <Img
        src={source.url}
        style={{width: '100%', height: '100%', objectFit: source.fit || 'cover'}}
      />
      <AbsoluteFill
        style={{
          background:
            'linear-gradient(180deg, rgba(0,0,0,0.08) 35%, rgba(0,0,0,0.72) 100%)',
        }}
      />
      {text?.text ? (
        <div
          style={{
            position: 'absolute',
            left: 56,
            right: 56,
            ...textPosition(text.position),
            color: palette.paper || '#FFFFFF',
            fontFamily: captions.font_family || 'Arial, Helvetica, sans-serif',
            fontSize: 76,
            fontWeight: 950,
            lineHeight: 0.98,
            letterSpacing: -2,
            textAlign: 'left',
            textTransform: 'uppercase',
            textShadow: '0 5px 16px ' + (palette.ink || '#000000'),
            WebkitTextStroke: '2px ' + (palette.ink || '#000000'),
            overflow: 'hidden',
            display: '-webkit-box',
            WebkitLineClamp: text.max_lines || 2,
            WebkitBoxOrient: 'vertical',
          }}
        >
          {text.text}
        </div>
      ) : null}
      <div
        style={{
          position: 'absolute', left: 0, bottom: 0, width: 18, height: '100%',
          background: palette.signal_blue || '#5B6CFF',
        }}
      />
    </AbsoluteFill>
  );
};