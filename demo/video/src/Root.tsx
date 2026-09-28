import React from 'react';
import {Composition} from 'remotion';
import {Teleporter} from './Teleporter';
import {FPS, TOTAL} from './theme';

export const RemotionRoot: React.FC = () => (
  <Composition id="Teleporter" component={Teleporter} durationInFrames={TOTAL} fps={FPS} width={1920} height={1080} />
);
