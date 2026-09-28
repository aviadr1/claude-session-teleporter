import React from 'react';
import {AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig} from 'remotion';
import {
  apply,
  appliedRows,
  appliedTitles,
  cells,
  CMD,
  copiedLine,
  personalTitleBefore,
  personalTitlesAfter,
  sessionRows,
  sessions,
} from '../captures';
import {AppMock, appSlot, SessionCard} from '../components/AppMock';
import {Caption} from '../components/Caption';
import {lineHeight, outputLineY, PAD, Terminal} from '../components/Terminal';
import {AGE} from '../appData';
import {C} from '../theme';

const APP = {x: 60, y: 200, w: 720, h: 820};
const T = {x: 820, y: 200, w: 1040, h: 820, fontSize: 17};
const rows = Math.floor((T.h - 44 - PAD * 2) / lineHeight(T.fontSize));
const clamp = {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'} as const;

const OUT = 36; // apply output starts, one line per frame
const FLY = 98; // first card leaves the terminal
const LAND = 132; // roughly when both have landed
const RELOAD = 150; // "switch accounts" in the app
const CLEAR = 166; // terminal moves on to `sessions -p personal`

export const Apply: React.FC = () => {
  const f = useCurrentFrame();
  const {fps} = useVideoConfig();

  const shown = interpolate(f, [OUT, OUT + apply.length], [0, apply.length], clamp);
  const scroll = Math.max(0, Math.round(shown) + 1 - rows);
  const finalScroll = Math.max(0, apply.length + 1 - rows);
  const charW = T.fontSize * 0.6;

  const reloaded = f >= RELOAD + 8;
  const org = f >= RELOAD && f < RELOAD + 8 ? 'work' : 'personal';
  const flash = f < RELOAD + 8 ? 0 : interpolate(f, [RELOAD + 8, RELOAD + 22], [1, 0], clamp);

  // the pre-existing session slides to its place in the final, recency-sorted list
  const settle = spring({frame: f - LAND + 10, fps, config: {damping: 18}});
  const blogSlot = interpolate(settle, [0, 1], [0, personalTitlesAfter.indexOf(personalTitleBefore)]);
  const blogPos = appSlot(APP, blogSlot);

  const cards = appliedTitles.map((title, k) => {
    const from = {
      x: T.x + PAD + 2 * charW,
      y: outputLineY({y: T.y, fontSize: T.fontSize, scroll: finalScroll}, appliedRows[k]) - 18,
    };
    const to = appSlot(APP, personalTitlesAfter.indexOf(title));
    const start = FLY + k * 9;
    const at = (frame: number) => spring({frame: frame - start, fps, config: {damping: 16, stiffness: 70}});
    const pos = (t: number) => ({
      x: from.x + (to.x - from.x) * t,
      y: from.y + (to.y - from.y) * t - Math.sin(Math.PI * Math.min(1, t)) * 160,
    });
    const t = at(f);
    if (f < start) return null;
    const p = pos(t);
    const settled = t > 0.98;
    const trail = [3, 6, 9].map((d) => {
      const tt = at(f - d);
      if (tt <= 0.02 || settled) return null;
      const q = pos(tt);
      return (
        <div key={d} style={{position: 'absolute', left: q.x, top: q.y, opacity: 0.25 - d * 0.02, filter: 'blur(3px)'}}>
          <SessionCard title={title} age={AGE[title]} style={{border: `1px solid ${C.cyan}`}} />
        </div>
      );
    });
    return (
      <React.Fragment key={title}>
        {trail}
        <div
          style={{
            position: 'absolute',
            left: p.x,
            top: p.y,
            transform: `scale(${0.85 + 0.15 * t}) rotate(${(1 - t) * -6}deg)`,
            opacity: reloaded ? 1 : settled ? 0.55 : 1,
          }}
        >
          <SessionCard
            title={title}
            age={AGE[title]}
            style={{
              border: reloaded ? `1px solid ${C.green}` : `2px dashed ${C.cyan}`,
              boxShadow: settled && !reloaded ? 'none' : `0 0 30px ${reloaded ? C.green : C.cyan}66`,
            }}
          />
        </div>
      </React.Fragment>
    );
  });

  const copiedInSessions = sessionRows.filter((i) => appliedTitles.includes(cells(sessions[i])[2]));

  return (
    <AbsoluteFill>
      <Caption kicker={`$ ${CMD.apply}`} to={126}>
        Apply it. They land in the org you're in.
      </Caption>
      <Caption kicker="then switch accounts once in the app" from={128} accent={C.green}>
        The list reloads, and there they are.
      </Caption>
      <AppMock {...APP} org={org} quota={88} items={[]} activeTitle={personalTitleBefore} flash={flash} />
      <div style={{position: 'absolute', left: blogPos.x, top: blogPos.y}}>
        <SessionCard title={personalTitleBefore} age={AGE[personalTitleBefore]} active />
      </div>
      {f < CLEAR ? (
        <Terminal
          {...T}
          command={CMD.apply}
          lines={apply}
          typeFrom={6}
          cps={1.6}
          outFrom={OUT}
          perLine={1}
          scroll={scroll}
          highlights={[
            ...appliedRows.map((line) => ({line, from: OUT + apply.length + 4, color: C.green})),
            {line: copiedLine, from: OUT + apply.length + 10, color: C.green},
          ]}
        />
      ) : (
        <Terminal
          {...T}
          fontSize={18}
          command={CMD.sessions}
          lines={sessions}
          typeFrom={CLEAR + 4}
          cps={1.6}
          outFrom={CLEAR + 30}
          perLine={2}
          highlights={copiedInSessions.map((line, k) => ({
            line,
            from: CLEAR + 52 + k * 4,
            color: C.green,
            label: 'copied from work',
          }))}
        />
      )}
      {cards}
    </AbsoluteFill>
  );
};
