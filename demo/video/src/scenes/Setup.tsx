import React from 'react';
import {AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig} from 'remotion';
import {personal, work} from '../captures';
import {quotaColor} from '../components/AppMock';
import {Caption} from '../components/Caption';
import {MONO, SANS} from '../fonts';
import {C} from '../theme';

// One login that belongs to two orgs, each with its own plan: that is what
// "two subscriptions" means in the fixture (and in real life). The account,
// org ids, session counts and quota come from the partitions capture.
const clamp = {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'} as const;
const pct = (cell: string) => Number(/(\d+)%$/.exec(cell)?.[1]);

const CARD_W = 600;
const CARD_H = 250;
const CARD_Y = 470;
const GAP = 120;
const LEFT_X = (1920 - CARD_W * 2 - GAP) / 2;
const RIGHT_X = LEFT_X + CARD_W + GAP;
const LOGIN = {x: 960, y: 300};

const SubscriptionCard: React.FC<{
  x: number;
  from: number;
  initial: string;
  accent: string;
  title: string;
  subtitle: string;
  org: string;
  sessions: string;
  quota: number;
  pulse?: boolean;
}> = ({x, from, initial, accent, title, subtitle, org, sessions, quota, pulse}) => {
  const f = useCurrentFrame();
  const {fps} = useVideoConfig();
  const s = spring({frame: f - from, fps, config: {damping: 16, stiffness: 130}});
  const qc = quotaColor(quota);
  const glow = pulse ? 0.5 + 0.5 * Math.sin(f / 3) : 0;
  return (
    <div
      style={{
        position: 'absolute',
        left: x,
        top: CARD_Y,
        width: CARD_W,
        height: CARD_H,
        boxSizing: 'border-box',
        padding: '30px 34px',
        borderRadius: 22,
        background: C.panel,
        border: `2px solid ${accent}66`,
        boxShadow: `0 30px 80px rgba(0,0,0,0.5)${pulse ? `, 0 0 ${30 * glow}px ${C.red}88` : ''}`,
        opacity: s,
        transform: `translateY(${(1 - s) * 40}px) scale(${0.94 + s * 0.06})`,
        fontFamily: SANS,
      }}
    >
      <div style={{display: 'flex', alignItems: 'center', gap: 18}}>
        <div
          style={{
            width: 56,
            height: 56,
            borderRadius: 28,
            background: accent,
            color: '#081018',
            fontWeight: 800,
            fontSize: 28,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
          }}
        >
          {initial}
        </div>
        <div>
          <div style={{fontSize: 40, fontWeight: 800, color: C.text, lineHeight: 1.1}}>{title}</div>
          <div style={{fontSize: 22, color: C.muted, marginTop: 4}}>{subtitle}</div>
        </div>
      </div>
      <div style={{fontFamily: MONO, fontSize: 19, color: C.dim, marginTop: 26}}>
        org {org} · {sessions}
      </div>
      <div style={{display: 'flex', alignItems: 'center', gap: 16, marginTop: 22}}>
        <div style={{fontSize: 17, color: C.muted, letterSpacing: 1.2, textTransform: 'uppercase', width: 120}}>usage left</div>
        <div style={{flex: 1, height: 16, borderRadius: 8, background: C.faint, overflow: 'hidden'}}>
          <div style={{width: `${quota}%`, height: '100%', background: qc, boxShadow: `0 0 18px ${qc}`}} />
        </div>
        <div style={{fontFamily: MONO, fontWeight: 700, fontSize: 30, color: qc, width: 80, textAlign: 'right'}}>
          {Math.round(quota)}%
        </div>
      </div>
    </div>
  );
};

export const Setup: React.FC = () => {
  const f = useCurrentFrame();
  const {fps} = useVideoConfig();
  const login = spring({frame: f - 2, fps, config: {damping: 16}});
  const draw = interpolate(f, [8, 26], [0, 1], clamp);
  const workQuota = interpolate(f, [30, 70], [46, pct(work[7])], clamp);
  const personalQuota = interpolate(f, [22, 44], [0, pct(personal[7])], clamp);
  const account = work[2];
  if (personal[2] !== account) throw new Error('the fixture should be one login in two orgs');

  const wire = (toX: number) => {
    const x2 = toX + CARD_W / 2;
    const y1 = LOGIN.y + 34;
    const y2 = CARD_Y;
    const midY = (y1 + y2) / 2;
    return `M ${LOGIN.x} ${y1} C ${LOGIN.x} ${midY}, ${x2} ${midY}, ${x2} ${y2}`;
  };

  return (
    <AbsoluteFill>
      <Caption kicker="the setup">One Claude login. Two subscriptions.</Caption>
      <svg width={1920} height={1080} style={{position: 'absolute', inset: 0}}>
        {[LEFT_X, RIGHT_X].map((x, k) => (
          <path
            key={k}
            d={wire(x)}
            fill="none"
            stroke={k === 0 ? C.violet : C.cyan}
            strokeWidth={3}
            strokeDasharray="1200"
            strokeDashoffset={1200 * (1 - draw)}
            opacity={0.7}
          />
        ))}
      </svg>
      <div
        style={{
          position: 'absolute',
          left: LOGIN.x,
          top: LOGIN.y,
          transform: `translate(-50%, -50%) scale(${0.9 + login * 0.1})`,
          opacity: login,
          padding: '16px 30px',
          borderRadius: 40,
          background: C.panelHi,
          border: `1px solid ${C.edge}`,
          fontFamily: SANS,
          fontSize: 26,
          fontWeight: 600,
          color: C.text,
          whiteSpace: 'nowrap',
        }}
      >
        one login <span style={{fontFamily: MONO, fontWeight: 400, color: C.muted, fontSize: 22}}>· account {account}</span>
      </div>
      <SubscriptionCard
        x={LEFT_X}
        from={12}
        initial="W"
        accent={C.violet}
        title="Work"
        subtitle="your company's Claude plan"
        org={work[1].slice(0, 8)}
        sessions={`${work[4]} sessions`}
        quota={workQuota}
        pulse={f > 70}
      />
      <SubscriptionCard
        x={RIGHT_X}
        from={20}
        initial="P"
        accent={C.cyan}
        title="Personal"
        subtitle="your own Claude plan"
        org={personal[1].slice(0, 8)}
        sessions={`${personal[4]} session`}
        quota={personalQuota}
      />
    </AbsoluteFill>
  );
};
