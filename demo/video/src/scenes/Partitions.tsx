import React from 'react';
import {AbsoluteFill, spring, useCurrentFrame, useVideoConfig} from 'remotion';
import {CMD, partitionsTable, personal, personalRow, work, workRow} from '../captures';
import {Caption} from '../components/Caption';
import {Terminal} from '../components/Terminal';
import {SANS} from '../fonts';
import {C} from '../theme';

const Chip: React.FC<{from: number; color: string; title: string; lines: string[]}> = ({from, color, title, lines}) => {
  const f = useCurrentFrame();
  const {fps} = useVideoConfig();
  const s = spring({frame: f - from, fps, config: {damping: 15, stiffness: 140}});
  return (
    <div
      style={{
        width: 700,
        padding: '22px 28px',
        borderRadius: 18,
        background: `${color}14`,
        border: `2px solid ${color}88`,
        fontFamily: SANS,
        opacity: s,
        transform: `translateY(${(1 - s) * 40}px) scale(${0.94 + s * 0.06})`,
      }}
    >
      <div style={{fontSize: 34, fontWeight: 800, color}}>{title}</div>
      {lines.map((l) => (
        <div key={l} style={{fontSize: 27, color: C.text, marginTop: 8}}>
          {l}
        </div>
      ))}
    </div>
  );
};

export const Partitions: React.FC = () => {
  const [wName, , , , wUnarch, , , wQuota] = work;
  const [pName, , , pAll, , , , pQuota] = personal;
  const quotaOf = (cell: string) => /(\d+%)$/.exec(cell)?.[1];
  return (
    <AbsoluteFill>
      <Caption kicker={`$ ${CMD.partitions}`} size={54}>They're not gone. They're in your other subscription.</Caption>
      <Terminal
        x={40}
        y={270}
        w={1840}
        h={280}
        fontSize={21}
        command={CMD.partitions}
        lines={partitionsTable}
        typeFrom={6}
        cps={1.3}
        outFrom={32}
        perLine={3}
        highlights={[
          {line: workRow, from: 56, color: C.red},
          {line: personalRow, from: 64, color: C.green},
        ]}
      />
      <div style={{position: 'absolute', top: 630, left: 0, right: 0, display: 'flex', justifyContent: 'center', gap: 60}}>
        <Chip
          from={62}
          color={C.red}
          title={`${wName} · your company's plan`}
          lines={[`${wUnarch} sessions you can't see`, `${quotaOf(wQuota)} quota left`]}
        />
        <Chip
          from={72}
          color={C.green}
          title={`● ${pName} · your plan, signed in`}
          lines={[`${pAll} session in the app`, `${quotaOf(pQuota)} quota left`]}
        />
      </div>
    </AbsoluteFill>
  );
};
