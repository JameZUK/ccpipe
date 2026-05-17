// Per-device list of recently-chosen project directories for the new-
// session flow. Stored client-side rather than on the server because the
// list is genuinely per-device — the dirs you use on phone aren't the
// same set you'd use on the desktop, and a sync round-trip isn't worth
// the complexity for a 10-element MRU cache.

const LS_KEY = "ccpipe.recentDirs";
const MAX_ENTRIES = 10;

export function loadRecentDirs(): string[] {
  try {
    const raw = localStorage.getItem(LS_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((v) => typeof v === "string").slice(0, MAX_ENTRIES);
  } catch {
    return [];
  }
}

export function pushRecentDir(path: string): void {
  if (!path || !path.startsWith("/")) return;
  try {
    const cur = loadRecentDirs().filter((p) => p !== path);
    cur.unshift(path);
    localStorage.setItem(LS_KEY, JSON.stringify(cur.slice(0, MAX_ENTRIES)));
  } catch {}
}
