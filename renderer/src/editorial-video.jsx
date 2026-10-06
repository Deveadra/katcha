import React from 'react';
import {AbsoluteFill, Audio, Freeze, Img, OffthreadVideo, Sequence, useCurrentFrame} from 'remotion';
import {containRect} from './editorial-contract.mjs';

const Annotation = ({overlay}) => {
  const r = overlay.region;
  return <div style={{position: 'absolute', left: `${r.x * 100}%`, top: `${r.y * 100}%`, width: `${r.width * 100}%`, height: `${r.height * 100}%`}}>
    {overlay.kind === 'arrow' ? <svg width="100%" height="100%" viewBox="0 0 100 100" preserveAspectRatio="none"><path d="M 5 5 L 88 88 M 48 88 L 88 88 L 88 48" fill="none" stroke="#ffe09a" strokeWidth="5" vectorEffect="non-scaling-stroke" /></svg> : <div style={{width: '100%', height: '100%', boxSizing: 'border-box', border: '5px solid #ffe09a', borderRadius: overlay.kind === 'circle' ? '50%' : 8, background: overlay.kind === 'highlight' ? '#ffe09a22' : 'transparent'}} />}
    {overlay.label && <div style={{position: 'absolute', top: 0, left: 0, transform: 'translateY(-100%)', color: '#fff', background: '#111e', padding: '5px 10px', fontSize: 24, maxWidth: 420, overflowWrap: 'anywhere'}}>{overlay.label}</div>}
  </div>;
};

const Scene = ({scene, assets, images}) => {
  const frame = useCurrentFrame();
  const caption = scene.captions.find(item => frame >= item.start_frame && frame < item.start_frame + item.duration_frames);
  const boxWidth = ['comparison', 'image_comparison'].includes(scene.layout) ? 912 : 1824;
  const boxHeight = 744;
  const imageIds = scene.layout === 'image' ? [scene.image_id] : (scene.image_ids || []);
  const imageZoom = 1 + ((scene.image_push_in || 1) - 1) * frame / Math.max(1, scene.duration_frames - 1);
  return <AbsoluteFill style={{background: '#101217', color: '#f8f8fa', fontFamily: 'Arial, sans-serif'}}>
    {['image', 'image_comparison'].includes(scene.layout) ? imageIds.map((id, index) => {
      const image = images.get(id);
      const rect = containRect(image.width, image.height, boxWidth, boxHeight);
      return <div key={id} style={{position: 'absolute', left: 48 + index * boxWidth, top: 96, width: boxWidth, height: boxHeight, overflow: 'hidden'}}>
        <div style={{position: 'absolute', ...rect, transform: `scale(${imageZoom})`}}>
          <Img src={image.url} style={{width: '100%', height: '100%'}} />
          {scene.overlays.filter(item => item.media_index === index).map((overlay, i) => <Annotation key={i} overlay={overlay} />)}
        </div>
        <div style={{position: 'absolute', bottom: 12, left: 24, right: 24, fontSize: 22, background: '#101217dd', padding: 8, overflowWrap: 'anywhere'}}>{image.illustration ? 'Illustration · ' : ''}{image.title}</div>
      </div>;
    }) : scene.layout === 'quote' ? <div style={{position: 'absolute', left: 160, right: 160, top: 140, height: 590, display: 'flex', flexDirection: 'column', justifyContent: 'center', gap: 36}}><div style={{fontSize: 54, lineHeight: 1.3, overflowWrap: 'anywhere'}}>“{scene.quote_text}”</div><div style={{fontSize: 28, color: '#b8bfce', overflowWrap: 'anywhere'}}>{scene.source_credit}</div></div> : scene.media.map((use, index) => {
      const asset = assets.get(use.candidate_id);
      const rect = containRect(asset.width, asset.height, boxWidth, boxHeight);
      const zoom = 1 + (use.push_in - 1) * frame / Math.max(1, scene.duration_frames - 1);
      const video = <OffthreadVideo src={asset.url} startFrom={Math.floor(use.start_seconds * 30)} playbackRate={use.playback_rate} muted style={{width: '100%', height: '100%'}} />;
      return <div key={index} style={{position: 'absolute', left: 48 + index * boxWidth, top: 96, width: boxWidth, height: boxHeight, overflow: 'hidden'}}><div style={{position: 'absolute', ...rect, transform: `scale(${zoom})`}}>
        {use.freeze ? <Freeze frame={0}>{video}</Freeze> : video}
        {scene.overlays.filter(item => item.media_index === index).map((overlay, i) => <Annotation key={i} overlay={overlay} />)}
      </div></div>;
    })}
    {scene.uncertainty_disclosure && <div style={{position: 'absolute', top: 24, left: 48, right: 48, fontSize: 26, lineHeight: 1.2, color: '#ffe09a'}}>{scene.uncertainty_disclosure}</div>}
    <div style={{position: 'absolute', left: 120, right: 120, top: 880, bottom: 40, display: 'flex', justifyContent: 'center', alignItems: 'center', textAlign: 'center', fontSize: (caption?.text.length || 0) > 120 ? 30 : 46, lineHeight: 1.25, overflowWrap: 'anywhere'}}>{caption?.text}</div>
  </AbsoluteFill>;
};

export const EditorialVideo = ({media, timeline, narration = [], images = []}) => {
  const assets = new Map(media.map(asset => [asset.candidate_id, asset]));
  const stills = new Map(images.map(image => [image.image_id, image]));
  const audio = new Map(narration.map(item => [item.beat_id, item]));
  return <AbsoluteFill>{timeline.map(scene => <Sequence key={scene.beat_id} from={scene.start_frame} durationInFrames={scene.duration_frames}><Scene scene={scene} assets={assets} images={stills} />{audio.has(scene.beat_id) && <Audio src={audio.get(scene.beat_id).url} />}</Sequence>)}</AbsoluteFill>;
};
