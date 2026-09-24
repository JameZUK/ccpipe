// "Recent" + "Recently modified" documents, shared by the Markdown
// viewer's drawer (viewer bundle) and the main app's docs dropdown.
// Recents live in localStorage — / and /view are the same origin, so a
// doc opened in the viewer shows up in the main app's menu too — keyed
// by project root (the session cwd, passed to /view as ?root=).

export const RECENT_SHOW = 5;
export const MODIFIED_SHOW = 5;
const RECENT_KEEP = 10;
const recentKey = (root: string) => `ccpipe.viewer.recent:${root}`;

/** Absolute paths of recently opened docs under *root*, newest first.
 *  May include files that no longer exist — callers filter against
 *  the current index. */
export function readRecents(root: string): string[] {
  try {
    const v = JSON.parse(localStorage.getItem(recentKey(root)) ?? "[]");
    return Array.isArray(v) ? v.filter((p): p is string => typeof p === "string") : [];
  } catch { return []; }
}

export function recordRecent(root: string, path: string): void {
  const list = [path, ...readRecents(root).filter((p) => p !== path)].slice(0, RECENT_KEEP);
  try { localStorage.setItem(recentKey(root), JSON.stringify(list)); } catch { /* ignore */ }
}

/** The *n* entries with the newest mtime (entries without one are skipped). */
export function recentlyModified<T extends { mtime?: number }>(entries: T[], n = MODIFIED_SHOW): T[] {
  return entries.filter((e) => e.mtime).sort((a, b) => b.mtime! - a.mtime!).slice(0, n);
}

/** Compact age for a unix timestamp: "now", "4m", "2h", "3d", "12 Mar". */
export function fmtAge(unix: number): string {
  const s = Math.max(0, Date.now() / 1000 - unix);
  if (s < 60) return "now";
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  if (s < 30 * 86400) return `${Math.floor(s / 86400)}d`;
  return new Date(unix * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short" });
}
