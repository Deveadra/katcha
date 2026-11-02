import React from 'react';
import {Composition, registerRoot} from 'remotion';
import {MemeTrack} from '../src/blueprint-video.jsx';

const Reaction = ({caption}) => <div style={{background: '#123456', flex: 1}}>
  <MemeTrack events={[{id: 'reaction', kind: 'image',
    url: 'data:image/svg+xml;base64,' + btoa('<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"><rect width="100" height="100" fill="red"/></svg>'),
    start_seconds: 0.5, duration_seconds: 1, volume: 0, caption}]} />
</div>;
registerRoot(() => <Composition id="CaptionReaction" component={Reaction}
  durationInFrames={60} fps={30} width={360} height={640} defaultProps={{caption: ''}} />);
