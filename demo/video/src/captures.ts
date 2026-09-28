// Real output of claude-sessions against a scratch fixture store, written by
// capture/capture.py. Nothing here is typed by hand: every terminal line the
// video shows is looked up in these files, and a lookup that finds nothing
// throws, so regenerated captures that no longer match fail the render.
import commands from './captures/commands.json';
import partitionsTxt from './captures/01-partitions.txt';
import dryRunTxt from './captures/02-copy-dry-run.txt';
import applyTxt from './captures/03-copy-apply.txt';
import sessionsTxt from './captures/05-sessions-personal.txt';

const toLines = (txt: string): string[] => txt.replace(/\r\n/g, '\n').replace(/\n+$/, '').split('\n');

export const find = (lines: string[], pred: (l: string) => boolean, what: string): number => {
  const i = lines.findIndex(pred);
  if (i < 0) throw new Error(`capture has no line for: ${what}`);
  return i;
};

export const findAll = (lines: string[], pred: (l: string) => boolean, what: string): number[] => {
  const hits = lines.flatMap((l, i) => (pred(l) ? [i] : []));
  if (hits.length === 0) throw new Error(`capture has no line for: ${what}`);
  return hits;
};

/** Split a table row on its column gaps (two or more spaces). */
export const cells = (row: string): string[] => row.trim().split(/\s{2,}/);

export const CMD = {
  partitions: commands['01-partitions'],
  dryRun: commands['02-copy-dry-run'],
  apply: commands['03-copy-apply'],
  sessions: commands['05-sessions-personal'],
};

export const partitions = toLines(partitionsTxt);
export const dryRun = toLines(dryRunTxt);
export const apply = toLines(applyTxt);
export const sessions = toLines(sessionsTxt);

// --- partitions: the table without the two store-path lines above it -------
const tableStart = find(partitions, (l) => l.startsWith('PARTITION'), 'partitions header');
export const partitionsTable = partitions.slice(tableStart);
export const workRow = find(partitionsTable, (l) => /^\s+work\s/.test(l), 'work row');
export const personalRow = find(partitionsTable, (l) => l.startsWith('● personal'), 'signed-in personal row');
export const work = cells(partitionsTable[workRow]);
export const personal = cells(partitionsTable[personalRow].replace('●', ''));

// --- dry run ----------------------------------------------------------------
export const arrowLine = find(dryRun, (l) => l.includes('═══▶'), 'SOURCE → TARGET arrow');
export const boxEnd = find(dryRun, (l) => l.startsWith('└'), 'bottom of the boxes');
export const actionHeader = find(dryRun, (l) => l.trim().startsWith('ACTION'), 'plan table header');
export const copyRows = findAll(dryRun, (l) => l.startsWith('✓  COPY'), 'COPY rows');
export const linearRemaps = findAll(dryRun, (l) => /remapped Linear.*01812872 ▶ 4b57c823/.test(l), 'Linear remap');
export const datadogDrops = findAll(dryRun, (l) => /dropped .*Datadog \(9d1a0b7e\)/.test(l), 'Datadog drop');
export const dryRunLast = find(dryRun, (l) => l.startsWith('DRY RUN.'), 'dry-run footer');

// --- apply ------------------------------------------------------------------
export const appliedRows = findAll(apply, (l) => /^✓ [0-9a-f]{8} {2}/.test(l), 'applied session lines');
export const copiedLine = find(apply, (l) => l.startsWith('Copied 2 session(s) into personal'), 'Copied line');
export const appliedTitles = appliedRows.map((i) => apply[i].replace(/^✓ [0-9a-f]{8} {2}/, ''));

// --- sessions -p personal ---------------------------------------------------
const sessionsHeader = find(sessions, (l) => l.startsWith('FLG'), 'sessions header');
export const sessionRows = sessions
  .map((l, i) => ({l, i}))
  .filter(({l, i}) => i > sessionsHeader + 1 && /^\s+[0-9a-f]{8}\s/.test(l))
  .map(({i}) => i);
if (sessionRows.length !== 3) throw new Error(`expected 3 personal sessions after apply, got ${sessionRows.length}`);
export const personalTitlesAfter = sessionRows.map((i) => cells(sessions[i])[2]);

// --- titles the stylised app shows, taken from the same captures ----------
export const workTitles = copyRows.map((i) => cells(dryRun[i])[4]);
export const personalTitleBefore = personalTitlesAfter.find((t) => !appliedTitles.includes(t)) as string;
if (!personalTitleBefore) throw new Error('no pre-existing personal session in the captures');
if (workTitles.join('|') !== appliedTitles.join('|')) throw new Error('dry run and apply disagree on the sessions');
