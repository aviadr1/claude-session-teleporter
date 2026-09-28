// The corner badge (name, URL, QR) sits bottom-right in every frame, so every
// scene keeps its windows above SAFE_BOTTOM.
export const REPO_URL = 'https://github.com/aviadr1/claude-session-teleporter';
export const REPO_SHORT = 'github.com/aviadr1/claude-session-teleporter';
export const TOOL_NAME = 'claude-session-teleporter';

export const QR_PX = 132; // 29 modules + a 2-module quiet zone each side = 33, at 4px each
export const BADGE = {w: 640, h: 156, margin: 22};
export const BADGE_TOP = 1080 - BADGE.margin - BADGE.h;
export const SAFE_BOTTOM = BADGE_TOP - 14;
