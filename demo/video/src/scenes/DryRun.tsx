import React from 'react';
import {AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig} from 'remotion';
import {
  arrowLine,
  boxEnd,
  CMD,
  copyRows,
  datadogDrops,
  dryRun,
  dryRunLast,
  linearRemaps,
} from '../captures';
import {Caption} from '../components/Caption';
import {Colorized} from '../components/colorize';
import {lineHeight, PAD, Terminal} from '../components/Terminal';
import {C} from '../theme';
import {SAFE_BOTTOM} from '../layout';

const T = {x: 160, y: 196, w: 1600, h: SAFE_BOTTOM - 196, fontSize: 24};
const rows = Math.floor((T.h - 44 - PAD * 2) / lineHeight(T.fontSize));
const clamp = {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'} as const;

const OUT = 30;
const ARROW_FROM = 62;
const ARROW_TO = 84;
const REST = 112; // lines after the boxes start here
const lineAt = (i: number) => (i <= boxEnd ? OUT + i * 2 : REST + (i - boxEnd) * 1.5);

// The arrow line is drawn as the captured text with its "═══ 2 ═══▶" segment
// revealed left to right - the same characters, blanked until they arrive.
const ArrowLine: React.FC<{frame: number}> = ({frame}) => {
  const line = dryRun[arrowLine];
  const m = /═+ \d+ ═+▶/.exec(line);
  if (!m) throw new Error('arrow segment missing from the capture');
  const s = m.index;
  const seg = m[0];
  const n = Math.round(interpolate(frame, [ARROW_FROM, ARROW_TO], [0, seg.length], clamp));
  const charW = T.fontSize * 0.6;
  const done = frame >= ARROW_TO;
  const packets = [0, 1].map((k) => {
    const t = interpolate(frame, [ARROW_TO + k * 8, ARROW_TO + 26 + k * 8], [0, 1], clamp);
    if (t <= 0 || t >= 1) return null;
    return (
      <span
        key={k}
        style={{
          position: 'absolute',
          left: (s + t * (seg.length - 1)) * charW,
          top: '50%',
          width: 10,
          height: 10,
          marginTop: -22,
          borderRadius: 5,
          background: '#fff',
          boxShadow: `0 0 18px 6px ${C.cyan}`,
        }}
      />
    );
  });
  return (
    <span style={{position: 'relative', display: 'inline-block'}}>
      <Colorized text={line.slice(0, s)} />
      <span
        style={{
          color: C.cyan,
          fontWeight: 700,
          textShadow: done ? `0 0 ${12 + 6 * Math.sin(frame / 4)}px ${C.cyan}` : `0 0 12px ${C.cyan}aa`,
        }}
      >
        {seg.slice(0, n)}
      </span>
      {' '.repeat(seg.length - n)}
      <Colorized text={line.slice(s + seg.length)} />
      {packets}
    </span>
  );
};

export const DryRun: React.FC = () => {
  const f = useCurrentFrame();
  const {fps} = useVideoConfig();
  const target = Math.max(0, dryRun.length + 1 - rows);
  const scroll = spring({frame: f - (REST + 6), fps, config: {damping: 22, stiffness: 70}}) * target;
  return (
    <AbsoluteFill>
      <Caption kicker={`$ ${CMD.dryRun}`} to={118} size={52}>
        Teleport them to your other subscription. Dry run first.
      </Caption>
      <Caption kicker="the part a plain cp gets wrong" from={120} accent={C.amber} size={52}>
        Connector IDs differ per subscription. It remaps them.
      </Caption>
      <Terminal
        {...T}
        command={CMD.dryRun}
        lines={dryRun}
        typeFrom={6}
        cps={1.6}
        outFrom={OUT}
        lineAt={lineAt}
        scroll={scroll}
        renderLine={(i, frame) => (i === arrowLine ? <ArrowLine frame={frame} /> : undefined)}
        highlights={[
          ...copyRows.map((line) => ({line, from: 138, color: C.green})),
          ...linearRemaps.map((line, k) => ({
            line,
            from: 150 + k * 4,
            color: C.cyan,
            label: k === 0 ? 'Linear has a new UUID in personal: remapped' : undefined,
          })),
          ...datadogDrops.map((line, k) => ({
            line,
            from: 166 + k * 4,
            color: C.amber,
            label: k === 0 ? 'no Datadog in personal: dropped' : undefined,
          })),
          {line: dryRunLast, from: 186, color: C.amber},
        ]}
      />
    </AbsoluteFill>
  );
};
