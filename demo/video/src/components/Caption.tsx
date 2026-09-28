import React from 'react';
import {interpolate, spring, useCurrentFrame, useVideoConfig} from 'remotion';
import {MONO, SANS} from '../fonts';
import {C} from '../theme';

/**
 * Headline at the top of a scene. `from`/`to` are local frames; the text
 * springs in word by word and fades out before `to`.
 */
export const Caption: React.FC<{
  kicker?: string;
  children: string;
  accent?: string;
  from?: number;
  to?: number;
  top?: number;
  size?: number;
}> = ({kicker, children, accent = C.cyan, from = 0, to = Infinity, top = 56, size = 58}) => {
  const f = useCurrentFrame();
  const {fps} = useVideoConfig();
  const out = to === Infinity ? 1 : interpolate(f, [to - 8, to], [1, 0], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});
  if (f < from || f > to) return null;
  const words = children.split(' ');
  return (
    <div style={{position: 'absolute', left: 0, right: 0, top, textAlign: 'center', opacity: out}}>
      {kicker ? (
        <div
          style={{
            fontFamily: MONO,
            fontSize: 22,
            letterSpacing: 3,
            textTransform: 'uppercase',
            color: accent,
            marginBottom: 12,
            opacity: interpolate(f - from, [0, 10], [0, 1], {extrapolateRight: 'clamp'}),
          }}
        >
          {kicker}
        </div>
      ) : null}
      <div style={{fontFamily: SANS, fontWeight: 800, fontSize: size, color: C.text, letterSpacing: -1.2}}>
        {words.map((w, i) => {
          const s = spring({frame: f - from - i * 2, fps, config: {damping: 18, stiffness: 160}});
          return (
            <span
              key={i}
              style={{
                display: 'inline-block',
                marginRight: '0.26em',
                opacity: s,
                transform: `translateY(${(1 - s) * 24}px)`,
              }}
            >
              {w}
            </span>
          );
        })}
      </div>
    </div>
  );
};
