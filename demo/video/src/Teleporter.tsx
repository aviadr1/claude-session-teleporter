import React from 'react';
import {AbsoluteFill, spring, useCurrentFrame, useVideoConfig} from 'remotion';
import {linearTiming, TransitionSeries} from '@remotion/transitions';
import {fade} from '@remotion/transitions/fade';
import {slide} from '@remotion/transitions/slide';
import './fonts';
import {Background} from './components/Background';
import {Badge} from './components/Badge';
import {Apply} from './scenes/Apply';
import {DryRun} from './scenes/DryRun';
import {EndCard} from './scenes/EndCard';
import {Pain} from './scenes/Pain';
import {Partitions} from './scenes/Partitions';
import {Setup} from './scenes/Setup';
import {SCENES, TOTAL, TRANSITION} from './theme';

const timing = linearTiming({durationInFrames: TRANSITION});
const END_START = TOTAL - SCENES.end;

/** The corner badge lives outside the scenes, so it is in every frame. */
const PersistentBadge: React.FC = () => {
  const f = useCurrentFrame();
  const {fps} = useVideoConfig();
  const grow = spring({frame: f - END_START - 50, fps, config: {damping: 18, stiffness: 90}});
  return <Badge grow={grow} />;
};

export const Teleporter: React.FC = () => (
  <AbsoluteFill>
    <Background />
    <TransitionSeries>
      <TransitionSeries.Sequence durationInFrames={SCENES.setup}>
        <Setup />
      </TransitionSeries.Sequence>
      <TransitionSeries.Transition presentation={fade()} timing={timing} />
      <TransitionSeries.Sequence durationInFrames={SCENES.pain}>
        <Pain />
      </TransitionSeries.Sequence>
      <TransitionSeries.Transition presentation={slide({direction: 'from-right'})} timing={timing} />
      <TransitionSeries.Sequence durationInFrames={SCENES.partitions}>
        <Partitions />
      </TransitionSeries.Sequence>
      <TransitionSeries.Transition presentation={slide({direction: 'from-bottom'})} timing={timing} />
      <TransitionSeries.Sequence durationInFrames={SCENES.dryRun}>
        <DryRun />
      </TransitionSeries.Sequence>
      <TransitionSeries.Transition presentation={slide({direction: 'from-right'})} timing={timing} />
      <TransitionSeries.Sequence durationInFrames={SCENES.apply}>
        <Apply />
      </TransitionSeries.Sequence>
      <TransitionSeries.Transition presentation={fade()} timing={timing} />
      <TransitionSeries.Sequence durationInFrames={SCENES.end}>
        <EndCard />
      </TransitionSeries.Sequence>
    </TransitionSeries>
    <PersistentBadge />
  </AbsoluteFill>
);
