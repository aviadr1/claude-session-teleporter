import React from 'react';
import {C} from '../theme';

// Colour only - never changes a character. Each rule styles the substrings it
// matches; the text that reaches the screen is exactly the captured line.
type Rule = [RegExp, React.CSSProperties];

const glow = (c: string) => `0 0 12px ${c}aa`;

const RULES: Rule[] = [
  [/═+ \d+ ═+▶/, {color: C.cyan, fontWeight: 700, textShadow: glow(C.cyan)}],
  [/[0-9a-f]{8} ▶ [0-9a-f]{8}/, {color: C.cyan, fontWeight: 700}],
  [/[┌┐└┘│]|─+/, {color: C.dim}],
  [/░+/, {color: '#5a3441'}],
  [/█+/, {color: C.green}],
  [/(?<=[░ ])2%/, {color: C.red, fontWeight: 700, textShadow: glow(C.red)}],
  [/88%/, {color: C.green, fontWeight: 700}],
  [/● signed in|●/, {color: C.green}],
  [/✓/, {color: C.green, fontWeight: 700}],
  [/✗ {2}skip: archived|✗/, {color: C.dim}],
  [/DRY RUN\. Nothing written\./, {color: C.amber, fontWeight: 700}],
  [/Copied \d+ session\(s\)/, {color: C.green, fontWeight: 700}],
  [/\b(SOURCE|TARGET)\b/, {color: C.muted, fontWeight: 700}],
];

const HEADER = /^\s*(PARTITION|ACTION|FLG)\s/;

export const Colorized: React.FC<{text: string}> = ({text}) => {
  if (HEADER.test(text)) return <span style={{color: C.muted}}>{text}</span>;
  const out: React.ReactNode[] = [];
  let rest = text;
  let key = 0;
  while (rest.length > 0) {
    let best: {i: number; len: number; style: React.CSSProperties} | null = null;
    for (const [re, style] of RULES) {
      const m = re.exec(rest);
      if (m && m[0].length > 0 && (best === null || m.index < best.i)) best = {i: m.index, len: m[0].length, style};
    }
    if (!best) {
      out.push(rest);
      break;
    }
    if (best.i > 0) out.push(rest.slice(0, best.i));
    out.push(
      <span key={key++} style={best.style}>
        {rest.slice(best.i, best.i + best.len)}
      </span>,
    );
    rest = rest.slice(best.i + best.len);
  }
  return <>{out}</>;
};
