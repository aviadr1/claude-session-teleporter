import React from 'react';
import {AbsoluteFill, useCurrentFrame} from 'remotion';
import {C} from '../theme';

/** Dark backdrop: two slow-drifting glows over a faint dot grid. */
export const Background: React.FC = () => {
  const f = useCurrentFrame();
  const a = 30 + Math.sin(f / 90) * 8;
  const b = 70 + Math.cos(f / 110) * 8;
  return (
    <AbsoluteFill style={{background: C.bg}}>
      <AbsoluteFill
        style={{
          background: `radial-gradient(900px 600px at ${a}% 20%, rgba(94,231,255,0.10), transparent 70%),
                       radial-gradient(900px 700px at ${b}% 85%, rgba(167,139,250,0.12), transparent 70%)`,
        }}
      />
      <AbsoluteFill
        style={{
          backgroundImage: 'radial-gradient(rgba(255,255,255,0.05) 1px, transparent 1px)',
          backgroundSize: '28px 28px',
          maskImage: 'radial-gradient(ellipse at center, black 40%, transparent 85%)',
        }}
      />
    </AbsoluteFill>
  );
};
