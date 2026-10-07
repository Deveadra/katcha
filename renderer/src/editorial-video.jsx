import React from 'react';
import {AbsoluteFill, Audio, Freeze, Img, OffthreadVideo, Sequence, useCurrentFrame} from 'remotion';
import {BrandLogo} from './brand-logo.jsx';
import {containRect, sourceViewport} from './editorial-contract.mjs';

const legacyBrand = {
  palette: {
    ink: '#101217',
    paper: '#f8f8fa',
    signal_blue: '#ffe09a',
    hot_peach: '#ffe09a',
    volt: '#ffe09a',
  },
  captions: {
    font_family: 'Arial, sans-serif',
    font_weight: 400,
  },
  logo: {enabled: false},
};

const resolveBrand = (brand) => brand
  ? {
      ...legacyBrand,
      ...brand,
      palette: {...legacyBrand.palette, ...(brand.palette || {})},
      captions: {...legacyBrand.captions, ...(brand.captions || {})},
      logo: {...legacyBrand.logo, ...(brand.logo || {})},
    }
  : legacyBrand;

const Annotation = ({overlay, brand}) => {
  const r = overlay.region;
  const accent = brand.palette.volt;
  return <div style={{position: 'absolute', left: `${r.x * 100}%`, top: `${r.y * 100}%`, width: `${r.width * 100}%`, height: `${r.height * 100}%`}}>
    {overlay.kind === 'arrow' ? <svg width="100%" height="100%" viewBox="0 0 100 100" preserveAspectRatio="none"><path d="M 5 5 L 88 88 M 48 88 L 88 88 L 88 48" fill="none" stroke={accent} strokeWidth="5" vectorEffect="non-scaling-stroke" /></svg> : <div style={{width: '100%', height: '100%', boxSizing: 'border-box', border: `5px solid ${accent}`, borderRadius: overlay.kind === 'circle' ? '50%' : 8, background: overlay.kind === 'highlight' ? accent : 'transparent', opacity: overlay.kind === 'highlight' ? 0.22 : 1}} />}
    {overlay.label && <div style={{position: 'absolute', top: 0, left: 0, transform: 'translateY(-100%)', color: brand.palette.paper, background: brand.palette.ink, padding: '5px 10px', fontSize: 24, maxWidth: 420, overflowWrap: 'anywhere'}}>{overlay.label}</div>}
  </div>;
};

const Scene = ({scene, assets, images, brand}) => {
  const frame = useCurrentFrame();
  const caption = scene.captions.find(item => frame >= item.start_frame && frame < item.start_frame + item.duration_frames);
  const boxWidth = ['comparison', 'image_comparison'].includes(scene.layout) ? 912 : 1824;
  const boxHeight = 744;
  const imageIds = scene.layout === 'image' ? [scene.image_id] : (scene.image_ids || []);
  const imageZoom = 1 + ((scene.image_push_in || 1) - 1) * frame / Math.max(1, scene.duration_frames - 1);
  const captionScale = scene.caption_scale || 1;
  const captionPosition = scene.caption_position || 'bottom';
  const transitionFrames = Math.min(scene.transition_frames || 8, Math.max(1, Math.floor(scene.duration_frames / 2)));
  const transitionDenominator = Math.max(1, transitionFrames - 1);
  const transitionOpacity = scene.transition === 'fade'
    ? Math.max(
        frame < transitionFrames ? 1 - frame / transitionDenominator : 0,
        frame >= scene.duration_frames - transitionFrames
          ? (frame - (scene.duration_frames - transitionFrames)) / transitionDenominator
          : 0,
      )
    : 0;
  return <AbsoluteFill style={{background: brand.palette.ink, color: brand.palette.paper, fontFamily: brand.captions.font_family}}>
    {['image', 'image_comparison'].includes(scene.layout) ? imageIds.map((id, index) => {
      const image = images.get(id);
      const rect = containRect(image.width, image.height, boxWidth, boxHeight);
      return <div key={id} style={{position: 'absolute', left: 48 + index * boxWidth, top: 96, width: boxWidth, height: boxHeight, overflow: 'hidden'}}>
        <div style={{position: 'absolute', ...rect, transform: `scale(${imageZoom})`}}>
          <Img src={image.url} style={{width: '100%', height: '100%'}} />
          {scene.overlays.filter(item => item.media_index === index).map((overlay, i) => <Annotation key={i} overlay={overlay} brand={brand} />)}
        </div>
        <div style={{position: 'absolute', bottom: 12, left: 24, right: 24, fontSize: 22, background: brand.palette.ink, color: brand.palette.paper, padding: 8, overflowWrap: 'anywhere'}}>{image.illustration ? 'Illustration · ' : ''}{image.title}</div>
      </div>;
    }) : scene.layout === 'quote' ? <div style={{position: 'absolute', left: 160, right: 160, top: 140, height: 590, display: 'flex', flexDirection: 'column', justifyContent: 'center', gap: 36}}><div style={{fontSize: 54, lineHeight: 1.3, overflowWrap: 'anywhere'}}>“{scene.quote_text}”</div><div style={{fontSize: 28, color: brand.palette.paper, opacity: 0.72, overflowWrap: 'anywhere'}}>{scene.source_credit}</div></div> : scene.media.map((use, index) => {
      const asset = assets.get(use.candidate_id);
      const geometry = sourceViewport(asset.width, asset.height, boxWidth, boxHeight, use.crop || null);
      const zoom = 1 + (use.push_in - 1) * frame / Math.max(1, scene.duration_frames - 1);
      const video = <OffthreadVideo src={asset.url} startFrom={Math.floor(use.start_seconds * 30)} playbackRate={use.playback_rate} muted style={{width: '100%', height: '100%'}} />;
      return <div key={index} style={{position: 'absolute', left: 48 + index * boxWidth, top: 96, width: boxWidth, height: boxHeight, overflow: 'hidden'}}>
        <div style={{position: 'absolute', ...geometry.viewport, overflow: 'hidden'}}>
          <div style={{
            position: 'absolute',
            ...geometry.content,
            transform: `scale(${zoom})`,
            transformOrigin: use.crop
              ? `${(use.crop.x + use.crop.width / 2) * 100}% ${(use.crop.y + use.crop.height / 2) * 100}%`
              : '50% 50%',
          }}>
            {use.freeze ? <Freeze frame={0}>{video}</Freeze> : video}
            {scene.overlays.filter(item => item.media_index === index).map((overlay, i) => <Annotation key={i} overlay={overlay} brand={brand} />)}
          </div>
        </div>
      </div>;
    })}
    {scene.uncertainty_disclosure && <div style={{position: 'absolute', top: 24, left: 48, right: 48, fontSize: 26, lineHeight: 1.2, color: brand.palette.volt}}>{scene.uncertainty_disclosure}</div>}
    <div style={{
      position: 'absolute',
      left: captionPosition === 'center' ? 220 : 120,
      right: captionPosition === 'center' ? 220 : 120,
      top: captionPosition === 'center' ? 430 : 880,
      bottom: captionPosition === 'center' ? 300 : 40,
      display: 'flex',
      justifyContent: 'center',
      alignItems: 'center',
      textAlign: 'center',
      fontSize: ((caption?.text.length || 0) > 120 ? 30 : 46) * captionScale,
      lineHeight: 1.25,
      fontWeight: brand.captions.font_weight,
      overflowWrap: 'anywhere',
      zIndex: 3,
    }}><span style={{
      background: scene.caption_background ? brand.palette.ink : 'transparent',
      padding: scene.caption_background ? '10px 18px' : 0,
      borderRadius: scene.caption_background ? 12 : 0,
    }}>{caption?.text}</span></div>
    {transitionOpacity > 0 && <AbsoluteFill style={{background: '#000', opacity: transitionOpacity, zIndex: 10}} />}
    <BrandLogo logo={{...brand.logo, opacity: (brand.logo.opacity ?? 0.9) * (1 - transitionOpacity)}} />
  </AbsoluteFill>;
};

export const EditorialVideo = ({media, timeline, narration = [], images = [], brand: brandInput = null}) => {
  const brand = resolveBrand(brandInput);
  const assets = new Map(media.map(asset => [asset.candidate_id, asset]));
  const stills = new Map(images.map(image => [image.image_id, image]));
  const audio = new Map(narration.map(item => [item.beat_id, item]));
  return <AbsoluteFill>{timeline.map(scene => <Sequence key={scene.beat_id} from={scene.start_frame} durationInFrames={scene.duration_frames}><Scene scene={scene} assets={assets} images={stills} brand={brand} />{audio.has(scene.beat_id) && <Audio src={audio.get(scene.beat_id).url} />}</Sequence>)}</AbsoluteFill>;
};
