import React from 'react';
import {MONO, SANS} from '../fonts';
import {C} from '../theme';
import {Window} from './Window';

// A stylised, generic desktop chat app: a session list, an org switcher and a
// usage meter. No real product's branding - it only has to read as "the app".

export type AppItem = {key: string; title: string; age: string; style?: React.CSSProperties};

export type AppProps = {
  x: number;
  y: number;
  w: number;
  h: number;
  org: string;
  quota: number;
  items: AppItem[];
  activeTitle: string;
  menu?: {open: number; hover?: string; orgs: string[]};
  ghosts?: number;
  ghostOpacity?: number;
  flash?: number;
  style?: React.CSSProperties;
};

export const SIDEBAR = 330;
const ITEM_TOP = 96;
export const ITEM_H = 64;
const ITEM_GAP = 10;

/** Absolute position of sidebar slot i, for things that fly into the list. */
export const appSlot = (a: {x: number; y: number}, i: number) => ({
  x: a.x + 16,
  y: a.y + 44 + ITEM_TOP + i * (ITEM_H + ITEM_GAP),
  w: SIDEBAR - 32,
  h: ITEM_H,
});

/** Absolute centre of the org switcher, for the cursor. */
export const appSwitcher = (a: {x: number; y: number; h: number}) => ({x: a.x + SIDEBAR / 2, y: a.y + a.h - 18 - 28});

/** Absolute centre of org row k in the open switcher menu. */
export const appMenuRow = (a: {x: number; y: number; h: number}, k: number, count: number) => ({
  x: a.x + SIDEBAR / 2,
  y: a.y + a.h - 18 - 70 - 6 - (count - k) * 44 + 22,
});

export const SessionCard: React.FC<{title: string; age: string; active?: boolean; style?: React.CSSProperties}> = ({
  title,
  age,
  active,
  style,
}) => (
  <div
    style={{
      width: SIDEBAR - 32,
      height: ITEM_H,
      borderRadius: 12,
      padding: '0 16px',
      display: 'flex',
      flexDirection: 'column',
      justifyContent: 'center',
      background: active ? '#1b2334' : '#121826',
      border: `1px solid ${active ? 'rgba(94,231,255,0.35)' : C.edge}`,
      fontFamily: SANS,
      boxSizing: 'border-box',
      ...style,
    }}
  >
    <div
      style={{fontSize: 19, fontWeight: 600, color: C.text, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis'}}
    >
      {title}
    </div>
    <div style={{fontSize: 14, color: C.muted, marginTop: 3}}>{age}</div>
  </div>
);

export const quotaColor = (q: number) => (q < 10 ? C.red : q < 35 ? C.amber : C.green);

export const AppMock: React.FC<AppProps> = (a) => {
  const qc = quotaColor(a.quota);
  return (
    <Window title="sessions" x={a.x} y={a.y} w={a.w} h={a.h} style={a.style} bodyStyle={{display: 'flex'}}>
      {/* sidebar */}
      <div style={{width: SIDEBAR, borderRight: `1px solid ${C.edge}`, background: '#0a0e15', position: 'relative'}}>
        <div
          style={{
            position: 'absolute',
            left: 16,
            right: 16,
            top: 22,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            fontFamily: SANS,
          }}
        >
          <div style={{fontSize: 22, fontWeight: 800, color: C.text}}>Sessions</div>
          <div
            style={{
              fontSize: 15,
              fontWeight: 600,
              color: C.muted,
              border: `1px solid ${C.edge}`,
              borderRadius: 8,
              padding: '5px 10px',
            }}
          >
            + New
          </div>
        </div>
        {Array.from({length: a.ghosts ?? 0}).map((_, i) => (
          <div
            key={`g${i}`}
            style={{
              position: 'absolute',
              left: 16,
              top: ITEM_TOP + (i + 1) * (ITEM_H + ITEM_GAP),
              width: SIDEBAR - 32,
              height: ITEM_H,
              borderRadius: 12,
              border: `2px dashed ${C.red}77`,
              color: `${C.red}cc`,
              fontFamily: MONO,
              fontSize: 26,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              boxSizing: 'border-box',
              opacity: a.ghostOpacity ?? 1,
            }}
          >
            ?
          </div>
        ))}
        {a.items.map((it, i) => (
          <div key={it.key} style={{position: 'absolute', left: 16, top: ITEM_TOP + i * (ITEM_H + ITEM_GAP)}}>
            <SessionCard title={it.title} age={it.age} active={it.title === a.activeTitle} style={it.style} />
          </div>
        ))}
        {/* org switcher */}
        <div style={{position: 'absolute', left: 16, right: 16, bottom: 18}}>
          {a.menu && a.menu.open > 0 ? (
            <div
              style={{
                position: 'absolute',
                left: 0,
                right: 0,
                bottom: 70,
                background: '#161d2b',
                border: `1px solid ${C.edge}`,
                borderRadius: 12,
                padding: 6,
                opacity: a.menu.open,
                transform: `translateY(${(1 - a.menu.open) * 12}px)`,
                boxShadow: '0 20px 50px rgba(0,0,0,0.5)',
              }}
            >
              {a.menu.orgs.map((o) => (
                <div
                  key={o}
                  style={{
                    height: 44,
                    padding: '0 12px',
                    borderRadius: 8,
                    background: a.menu?.hover === o ? 'rgba(94,231,255,0.16)' : 'transparent',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                    fontFamily: SANS,
                    fontSize: 18,
                    fontWeight: 600,
                    color: C.text,
                  }}
                >
                  <span>{o}</span>
                  <span style={{color: C.green}}>{o === a.org ? '✓' : ''}</span>
                </div>
              ))}
            </div>
          ) : null}
          <div
            style={{
              height: 56,
              borderRadius: 12,
              border: `1px solid ${C.edge}`,
              background: '#121826',
              display: 'flex',
              alignItems: 'center',
              padding: '0 12px',
              gap: 12,
              fontFamily: SANS,
            }}
          >
            <div
              style={{
                width: 32,
                height: 32,
                borderRadius: 16,
                background: a.org === 'work' ? C.violet : C.cyan,
                color: '#081018',
                fontWeight: 800,
                fontSize: 17,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
              }}
            >
              {a.org[0].toUpperCase()}
            </div>
            <div style={{flex: 1}}>
              <div style={{fontSize: 13, color: C.muted}}>org</div>
              <div style={{fontSize: 18, fontWeight: 600, color: C.text}}>{a.org}</div>
            </div>
            <div style={{color: C.muted, fontSize: 16}}>▾</div>
          </div>
        </div>
      </div>
      {/* main pane */}
      <div style={{flex: 1, position: 'relative', background: C.panel}}>
        <div
          style={{
            position: 'absolute',
            left: 28,
            right: 28,
            top: 22,
            display: 'flex',
            flexWrap: 'wrap',
            rowGap: 14,
            justifyContent: 'space-between',
            alignItems: 'center',
            fontFamily: SANS,
          }}
        >
          <div style={{fontSize: 22, fontWeight: 700, color: C.text, whiteSpace: 'nowrap'}}>{a.activeTitle}</div>
          <div style={{display: 'flex', alignItems: 'center', gap: 12}}>
            <div style={{fontSize: 14, color: C.muted, letterSpacing: 1, textTransform: 'uppercase'}}>usage left</div>
            <div style={{width: 150, height: 12, borderRadius: 6, background: C.faint, overflow: 'hidden'}}>
              <div style={{width: `${a.quota}%`, height: '100%', background: qc, boxShadow: `0 0 16px ${qc}`}} />
            </div>
            <div style={{fontFamily: MONO, fontWeight: 700, fontSize: 20, color: qc, width: 56, textAlign: 'right'}}>
              {Math.round(a.quota)}%
            </div>
          </div>
        </div>
        {[0.62, 0.8, 0.45, 0.7].map((wd, i) => (
          <div
            key={i}
            style={{
              position: 'absolute',
              top: 100 + i * 96,
              [i % 2 ? 'right' : 'left']: 28,
              width: `${wd * 70}%`,
              height: 64,
              borderRadius: 14,
              background: i % 2 ? 'rgba(167,139,250,0.10)' : '#131a27',
              border: `1px solid ${C.edge}`,
              padding: '16px 20px',
              boxSizing: 'border-box',
            }}
          >
            <div style={{height: 10, width: '85%', borderRadius: 5, background: 'rgba(255,255,255,0.10)'}} />
            <div style={{height: 10, width: '55%', borderRadius: 5, background: 'rgba(255,255,255,0.07)', marginTop: 10}} />
          </div>
        ))}
        <div
          style={{
            position: 'absolute',
            left: 28,
            right: 28,
            bottom: 24,
            height: 60,
            borderRadius: 14,
            border: `1px solid ${C.edge}`,
            background: '#0a0e15',
          }}
        />
      </div>
      {a.flash ? (
        <div style={{position: 'absolute', inset: 0, background: `rgba(94,231,255,${a.flash * 0.14})`, pointerEvents: 'none'}} />
      ) : null}
    </Window>
  );
};

export const Cursor: React.FC<{x: number; y: number; press?: number}> = ({x, y, press = 0}) => (
  <svg
    width={34}
    height={40}
    viewBox="0 0 34 40"
    style={{
      position: 'absolute',
      left: x,
      top: y,
      transform: `scale(${1 - press * 0.15})`,
      transformOrigin: '0 0',
      filter: 'drop-shadow(0 6px 10px rgba(0,0,0,0.6))',
    }}
  >
    <path d="M2 2 L2 32 L10 25 L16 38 L22 35 L16 23 L27 23 Z" fill="#fff" stroke="#111" strokeWidth={2} strokeLinejoin="round" />
  </svg>
);
