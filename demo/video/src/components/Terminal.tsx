import React from 'react';
import {interpolate, spring, useCurrentFrame, useVideoConfig} from 'remotion';
import {MONO} from '../fonts';
import {C} from '../theme';
import {Colorized} from './colorize';
import {Window} from './Window';

export type Highlight = {line: number; from: number; color: string; label?: string};

export type TerminalProps = {
  x: number;
  y: number;
  w: number;
  h: number;
  fontSize: number;
  command: string;
  lines: string[];
  /** frame the command starts typing, and characters typed per frame */
  typeFrom: number;
  cps?: number;
  /** frame each output line appears; defaults to outFrom + i * perLine */
  outFrom: number;
  perLine?: number;
  lineAt?: (i: number) => number;
  /** first visible row (0 = the prompt line), may be fractional */
  scroll?: number;
  highlights?: Highlight[];
  /** show a line as a same-width variant, e.g. partially drawn; must return undefined for "as captured" */
  renderLine?: (i: number, frame: number) => React.ReactNode | undefined;
  style?: React.CSSProperties;
};

export const PAD = 26;
export const lineHeight = (fontSize: number) => Math.round(fontSize * 1.45);

/** y of output line i's top edge, in the same coordinates as the terminal's x/y. */
export const outputLineY = (p: {y: number; fontSize: number; scroll?: number}, i: number) =>
  p.y + 44 + PAD + (i + 1 - (p.scroll ?? 0)) * lineHeight(p.fontSize);

export const Terminal: React.FC<TerminalProps> = (p) => {
  const f = useCurrentFrame();
  const {fps} = useVideoConfig();
  const lh = lineHeight(p.fontSize);
  const cps = p.cps ?? 1.4;
  const typed = Math.max(0, Math.min(p.command.length, Math.floor((f - p.typeFrom) * cps)));
  const at = p.lineAt ?? ((i: number) => p.outFrom + i * (p.perLine ?? 2));
  const cursorOn = f < p.outFrom && Math.floor(f / 8) % 2 === 0;
  const charW = p.fontSize * 0.6;
  const chipH = Math.min(38, lh + 4);

  return (
    <Window title="~ — claude-sessions" x={p.x} y={p.y} w={p.w} h={p.h} style={p.style}>
      <div
        style={{
          position: 'absolute',
          left: PAD,
          right: PAD,
          top: PAD,
          transform: `translateY(${-(p.scroll ?? 0) * lh}px)`,
          fontFamily: MONO,
          fontSize: p.fontSize,
          lineHeight: `${lh}px`,
          color: C.text,
          whiteSpace: 'pre',
          fontVariantLigatures: 'none',
        }}
      >
        {(p.highlights ?? []).map((h, k) => {
          const s = spring({frame: f - h.from, fps, config: {damping: 20, stiffness: 140}});
          if (f < h.from) return null;
          return (
            <div
              key={k}
              style={{
                position: 'absolute',
                left: -12,
                top: (h.line + 1) * lh,
                height: lh,
                width: `calc(${s * 100}% + 24px)`,
                background: `${h.color}1f`,
                borderLeft: `3px solid ${h.color}`,
                borderRadius: 4,
              }}
            />
          );
        })}
        <div>
          <span style={{color: C.prompt, fontWeight: 700}}>$ </span>
          <span>{p.command.slice(0, typed)}</span>
          {f < p.outFrom ? (
            <span
              style={{
                display: 'inline-block',
                width: charW,
                height: lh * 0.8,
                verticalAlign: 'middle',
                background: cursorOn ? C.text : 'transparent',
              }}
            />
          ) : null}
        </div>
        {p.lines.map((text, i) => {
          const t = at(i);
          if (f < t) return <div key={i}>&nbsp;</div>;
          const o = interpolate(f, [t, t + 4], [0, 1], {extrapolateRight: 'clamp'});
          const custom = p.renderLine?.(i, f);
          return (
            <div key={i} style={{opacity: o, transform: `translateY(${(1 - o) * 6}px)`}}>
              {custom ?? (text.length ? <Colorized text={text} /> : ' ')}
            </div>
          );
        })}
        {(p.highlights ?? [])
          .filter((h) => h.label && f >= h.from + 6)
          .map((h, k) => {
            const s = spring({frame: f - h.from - 6, fps, config: {damping: 16, stiffness: 150}});
            return (
              <div
                key={`l${k}`}
                style={{
                  position: 'absolute',
                  right: 0,
                  top: (h.line + 1) * lh + (lh - chipH) / 2,
                  height: chipH,
                  padding: '0 16px',
                  display: 'flex',
                  alignItems: 'center',
                  borderRadius: chipH / 2,
                  background: h.color,
                  color: '#081018',
                  fontFamily: 'Inter',
                  fontWeight: 800,
                  fontSize: Math.round(chipH * 0.52),
                  whiteSpace: 'nowrap',
                  opacity: s,
                  transform: `translateX(${(1 - s) * 40}px)`,
                  boxShadow: `0 8px 30px ${h.color}55`,
                }}
              >
                {h.label}
              </div>
            );
          })}
      </div>
    </Window>
  );
};
