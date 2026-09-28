export const C = {
  bg: '#06080d',
  panel: '#0d1119',
  panelHi: '#131926',
  edge: 'rgba(255,255,255,0.09)',
  text: '#e8ebf2',
  muted: '#8b94a7',
  dim: '#4b5466',
  faint: '#262d3b',
  cyan: '#5ee7ff',
  violet: '#a78bfa',
  green: '#4ade80',
  red: '#ff5d6c',
  amber: '#fbbf24',
  prompt: '#a78bfa',
};

export const FPS = 30;

/** Frames per scene, before transitions overlap them. */
export const SCENES = {
  pain: 170,
  partitions: 140,
  dryRun: 225,
  apply: 245,
  end: 150,
};
export const TRANSITION = 15;
export const TOTAL =
  Object.values(SCENES).reduce((a, b) => a + b, 0) - TRANSITION * (Object.keys(SCENES).length - 1);
