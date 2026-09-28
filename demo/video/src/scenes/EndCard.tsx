import React from 'react';
import {AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig} from 'remotion';
import {MONO, SANS} from '../fonts';
import {C} from '../theme';

const REPO = 'github.com/aviadr1/claude-session-teleporter';
const INSTALL = 'uv tool install claude-session-teleporter';

const Rings: React.FC = () => {
  const f = useCurrentFrame();
  return (
    <AbsoluteFill style={{alignItems: 'center', justifyContent: 'center'}}>
      {[0, 1, 2, 3].map((k) => {
        const t = ((f + k * 22) % 88) / 88;
        return (
          <div
            key={k}
            style={{
              position: 'absolute',
              width: 300 + t * 1500,
              height: (300 + t * 1500) * 0.42,
              borderRadius: '50%',
              border: `2px solid ${k % 2 ? C.violet : C.cyan}`,
              opacity: (1 - t) * 0.35,
            }}
          />
        );
      })}
    </AbsoluteFill>
  );
};

export const EndCard: React.FC = () => {
  const f = useCurrentFrame();
  const {fps} = useVideoConfig();
  const pop = (d: number) => spring({frame: f - d, fps, config: {damping: 14, stiffness: 120}});
  // starts after the cross-fade in, so the headline never overlaps the last scene
  const a = pop(16);
  const b = pop(32);
  const c = pop(46);
  const d = pop(58);
  const typed = Math.floor(interpolate(f, [50, 50 + INSTALL.length / 1.8], [0, INSTALL.length], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'}));

  return (
    <AbsoluteFill style={{alignItems: 'center', justifyContent: 'center'}}>
      <Rings />
      <div style={{textAlign: 'center', marginTop: -40}}>
        <div
          style={{
            fontFamily: SANS,
            fontWeight: 800,
            fontSize: 110,
            letterSpacing: -3,
            color: C.text,
            opacity: a,
            transform: `translateY(${(1 - a) * 50}px) scale(${0.9 + a * 0.1})`,
          }}
        >
          Oh, you{' '}
          <span
            style={{
              fontStyle: 'italic',
              background: `linear-gradient(90deg, ${C.cyan}, ${C.violet})`,
              WebkitBackgroundClip: 'text',
              backgroundClip: 'text',
              color: 'transparent',
              paddingRight: 8,
            }}
          >
            can
          </span>{' '}
          take it with you.
        </div>
        <div
          style={{
            fontFamily: SANS,
            fontWeight: 600,
            fontSize: 44,
            color: C.muted,
            marginTop: 18,
            opacity: b,
            transform: `translateY(${(1 - b) * 30}px)`,
          }}
        >
          Out of quota, not out of context.
        </div>
        <div
          style={{
            display: 'inline-flex',
            alignItems: 'center',
            marginTop: 64,
            padding: '22px 36px',
            borderRadius: 18,
            background: C.panel,
            border: `1px solid ${C.edge}`,
            boxShadow: `0 0 60px rgba(94,231,255,0.15)`,
            fontFamily: MONO,
            fontSize: 40,
            color: C.text,
            whiteSpace: 'pre',
            opacity: c,
            transform: `scale(${0.92 + c * 0.08})`,
          }}
        >
          <span style={{color: C.prompt, fontWeight: 700}}>$ </span>
          {INSTALL.slice(0, typed)}
          <span style={{color: 'transparent'}}>{INSTALL.slice(typed)}</span>
        </div>
        <div
          style={{
            fontFamily: MONO,
            fontSize: 34,
            color: C.cyan,
            marginTop: 40,
            opacity: d,
            transform: `translateY(${(1 - d) * 20}px)`,
          }}
        >
          {REPO}
        </div>
      </div>
    </AbsoluteFill>
  );
};
