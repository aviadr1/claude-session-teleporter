import React from 'react';
import {AbsoluteFill, interpolate, random, spring, useCurrentFrame, useVideoConfig} from 'remotion';
import {personal, personalTitleBefore, work, workTitles} from '../captures';
import {AppMock, appMenuRow, appSlot, appSwitcher, Cursor} from '../components/AppMock';
import {Caption} from '../components/Caption';
import {AGE} from '../appData';
import {C} from '../theme';
import {SAFE_BOTTOM} from '../layout';

const APP = {x: 260, y: 196, w: 1400, h: SAFE_BOTTOM - 196};
const ORGS = ['work', 'personal'];
const pct = (cell: string) => Number(/(\d+)%$/.exec(cell)?.[1]);
const WORK_QUOTA = pct(work[7]);
const PERSONAL_QUOTA = pct(personal[7]);

const SWITCH = 72; // the frame the org flips
const clamp = {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'} as const;

const lerpPt = (a: {x: number; y: number}, b: {x: number; y: number}, t: number) => ({
  x: a.x + (b.x - a.x) * t,
  y: a.y + (b.y - a.y) * t,
});

const Dust: React.FC<{i: number; start: number}> = ({i, start}) => {
  const f = useCurrentFrame();
  const t = f - start;
  if (t < 0 || t > 34) return null;
  const slot = appSlot(APP, i);
  return (
    <>
      {Array.from({length: 18}).map((_, k) => {
        const sx = slot.x + random(`x${i}${k}`) * slot.w;
        const sy = slot.y + random(`y${i}${k}`) * slot.h;
        const vx = (random(`vx${i}${k}`) - 0.3) * 7;
        const vy = -1.5 - random(`vy${i}${k}`) * 4;
        const size = 4 + random(`s${i}${k}`) * 8;
        return (
          <div
            key={k}
            style={{
              position: 'absolute',
              left: sx + vx * t,
              top: sy + vy * t,
              width: size,
              height: size,
              borderRadius: 2,
              background: k % 3 ? C.violet : C.cyan,
              opacity: interpolate(t, [0, 34], [0.9, 0]),
              boxShadow: `0 0 10px ${C.violet}`,
            }}
          />
        );
      })}
    </>
  );
};

export const Pain: React.FC = () => {
  const f = useCurrentFrame();
  const {fps} = useVideoConfig();
  const enter = spring({frame: f, fps, config: {damping: 18}});
  const switched = f >= SWITCH;

  const drain = interpolate(f, [0, 16], [6, WORK_QUOTA], clamp);
  const refill = interpolate(f, [SWITCH, SWITCH + 26], [WORK_QUOTA, PERSONAL_QUOTA], clamp);
  const quota = switched ? refill : drain;

  // cursor: drift to the switcher, open it, pick personal
  const home = {x: 1200, y: 700};
  const sw = appSwitcher(APP);
  const row = appMenuRow(APP, 1, ORGS.length);
  const toSwitcher = spring({frame: f - 12, fps, config: {damping: 20, stiffness: 90}});
  const toRow = spring({frame: f - 54, fps, config: {damping: 20, stiffness: 120}});
  const cur = f < 54 ? lerpPt(home, sw, toSwitcher) : lerpPt(sw, row, toRow);
  const press =
    interpolate(f, [42, 45, 49], [0, 1, 0], clamp) + interpolate(f, [SWITCH - 4, SWITCH - 1, SWITCH + 3], [0, 1, 0], clamp);
  const menuOpen = f >= 46 && f < SWITCH ? spring({frame: f - 46, fps, config: {damping: 16}}) : 0;

  const items = switched
    ? [
        {
          key: 'p',
          title: personalTitleBefore,
          age: AGE[personalTitleBefore],
          style: {opacity: interpolate(f, [SWITCH + 16, SWITCH + 28], [0, 1], clamp)},
        },
      ]
    : workTitles.map((title, i) => ({key: `w${i}`, title, age: AGE[title]}));

  // the work cards, dissolving where they stood
  const ghostsOf = switched
    ? workTitles.map((title, i) => {
        const t = f - SWITCH - i * 5;
        const o = interpolate(t, [0, 16], [1, 0], clamp);
        return (
          <div key={title} style={{position: 'absolute', left: appSlot(APP, i).x, top: appSlot(APP, i).y}}>
            <div
              style={{
                opacity: o,
                filter: `blur(${(1 - o) * 8}px)`,
                transform: `translateX(${(1 - o) * -60}px) scale(${0.9 + o * 0.1})`,
              }}
            >
              <div
                style={{
                  width: appSlot(APP, i).w,
                  height: appSlot(APP, i).h,
                  borderRadius: 12,
                  background: '#121826',
                  border: `1px solid ${C.edge}`,
                  color: C.text,
                  fontFamily: 'Inter',
                  fontSize: 19,
                  fontWeight: 600,
                  padding: '0 16px',
                  boxSizing: 'border-box',
                  display: 'flex',
                  alignItems: 'center',
                }}
              >
                {title}
              </div>
            </div>
          </div>
        );
      })
    : null;

  const ghosts = interpolate(f, [SWITCH + 30, SWITCH + 42], [0, 1], clamp);

  return (
    <AbsoluteFill>
      <Caption kicker="the problem" to={SWITCH - 2} accent={C.red}>
        {`Work hits ${WORK_QUOTA}% mid-task.`}
      </Caption>
      <Caption kicker="so you switch to personal" from={SWITCH} accent={C.violet}>
        …and your work sessions aren't there.
      </Caption>
      <div style={{position: 'absolute', inset: 0, opacity: enter, transform: `scale(${0.96 + enter * 0.04})`}}>
        <AppMock
          {...APP}
          org={switched ? 'personal' : 'work'}
          quota={quota}
          items={items}
          activeTitle={switched ? personalTitleBefore : workTitles[0]}
          menu={{open: menuOpen, hover: toRow > 0.6 ? 'personal' : undefined, orgs: ORGS}}
          ghosts={switched ? workTitles.length : 0}
          ghostOpacity={ghosts * (0.75 + 0.25 * Math.sin(f / 4))}
          flash={switched ? interpolate(f, [SWITCH, SWITCH + 12], [1, 0], clamp) : 0}
        />
        {ghostsOf}
        {switched ? workTitles.map((_, i) => <Dust key={i} i={i} start={SWITCH + i * 5} />) : null}
        {!switched && quota <= WORK_QUOTA + 0.01 ? (
          <div
            style={{
              position: 'absolute',
              left: APP.x + APP.w - 108,
              top: APP.y + 44 + 22 - 14,
              width: 96,
              height: 52,
              borderRadius: 26,
              border: `2px solid ${C.red}`,
              opacity: 0.5 + 0.5 * Math.sin(f / 3),
              boxShadow: `0 0 24px ${C.red}`,
            }}
          />
        ) : null}
        {f < SWITCH + 20 ? (
          <Cursor x={cur.x} y={cur.y} press={Math.min(1, press)} />
        ) : null}
      </div>
    </AbsoluteFill>
  );
};
