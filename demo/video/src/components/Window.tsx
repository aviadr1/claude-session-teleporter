import React from 'react';
import {MONO} from '../fonts';
import {C} from '../theme';

/** A generic desktop window frame: neutral traffic dots, a title, a body. */
export const Window: React.FC<{
  title: string;
  x: number;
  y: number;
  w: number;
  h: number;
  style?: React.CSSProperties;
  bodyStyle?: React.CSSProperties;
  children: React.ReactNode;
}> = ({title, x, y, w, h, style, bodyStyle, children}) => (
  <div
    style={{
      position: 'absolute',
      left: x,
      top: y,
      width: w,
      height: h,
      borderRadius: 16,
      background: C.panel,
      border: `1px solid ${C.edge}`,
      boxShadow: '0 40px 120px rgba(0,0,0,0.55), 0 0 0 1px rgba(0,0,0,0.4)',
      overflow: 'hidden',
      display: 'flex',
      flexDirection: 'column',
      ...style,
    }}
  >
    <div
      style={{
        height: 44,
        flex: '0 0 44px',
        display: 'flex',
        alignItems: 'center',
        padding: '0 18px',
        background: C.panelHi,
        borderBottom: `1px solid ${C.edge}`,
        position: 'relative',
      }}
    >
      {[0, 1, 2].map((i) => (
        <div key={i} style={{width: 13, height: 13, borderRadius: 7, background: '#3a4152', marginRight: 9}} />
      ))}
      <div
        style={{
          position: 'absolute',
          left: 0,
          right: 0,
          textAlign: 'center',
          fontFamily: MONO,
          fontSize: 17,
          color: C.muted,
          pointerEvents: 'none',
        }}
      >
        {title}
      </div>
    </div>
    <div style={{flex: 1, position: 'relative', overflow: 'hidden', ...bodyStyle}}>{children}</div>
  </div>
);
