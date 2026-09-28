import React from 'react';
import {Img} from 'remotion';

export const BrandLogo = ({logo}) => {
  if (!logo?.enabled || !logo?.url) return null;
  return (
    <Img
      src={logo.url}
      style={{
        position: 'absolute',
        left: `${logo.x_percent ?? 88}%`,
        top: `${logo.y_percent ?? 8}%`,
        width: `${logo.width_percent ?? 13}%`,
        height: 'auto',
        transform: 'translate(-50%, -50%)',
        opacity: logo.opacity ?? 0.9,
        zIndex: 30,
        pointerEvents: 'none',
      }}
    />
  );
};
