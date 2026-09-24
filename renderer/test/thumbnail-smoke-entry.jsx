import React from 'react';
import {Composition, registerRoot, staticFile} from 'remotion';
import {Thumbnail} from '../src/thumbnail.jsx';

const props = {
  source: {url: staticFile('thumbnail-source.png'), fit: 'cover'},
  text: {text: 'NO WAY THAT WORKED', position: 'bottom', max_lines: 2},
  brand: {
    palette: {ink: '#101216', paper: '#F6F3EC', signal_blue: '#5B6CFF'},
    captions: {font_family: 'Arial, Helvetica, sans-serif'},
  },
};

const Root = () => (
  <Composition
    id="SyntheticThumbnail"
    component={Thumbnail}
    durationInFrames={1}
    fps={30}
    width={1280}
    height={720}
    defaultProps={props}
  />
);

registerRoot(Root);