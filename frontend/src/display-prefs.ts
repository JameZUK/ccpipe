// Per-device display preferences for the terminal. Stored in
// localStorage so each browser/device has its own — these aren't worth
// syncing through the server.

const LS_KEY = "ccpipe.display.v1";

export type CursorStyle = "bar" | "block" | "underline";

export interface DisplayPrefs {
  fontSize: number;        // 11..22 px
  lineHeight: number;      // 1.0..1.6
  letterSpacing: number;   // 0..3 px
  cursorStyle: CursorStyle;
  cursorBlink: boolean;
}

export const DEFAULT_PREFS: DisplayPrefs = {
  fontSize: 14,
  lineHeight: 1.15,
  letterSpacing: 0,
  cursorStyle: "bar",
  cursorBlink: true,
};

function clamp(n: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, n));
}

function sanitize(p: Partial<DisplayPrefs>): DisplayPrefs {
  return {
    fontSize: clamp(Math.round(p.fontSize ?? DEFAULT_PREFS.fontSize), 11, 22),
    lineHeight: clamp(Number(p.lineHeight ?? DEFAULT_PREFS.lineHeight), 1.0, 1.6),
    letterSpacing: clamp(Number(p.letterSpacing ?? DEFAULT_PREFS.letterSpacing), 0, 3),
    cursorStyle: (["bar", "block", "underline"] as const).includes(p.cursorStyle as CursorStyle)
      ? (p.cursorStyle as CursorStyle)
      : DEFAULT_PREFS.cursorStyle,
    cursorBlink: typeof p.cursorBlink === "boolean" ? p.cursorBlink : DEFAULT_PREFS.cursorBlink,
  };
}

export function loadDisplayPrefs(session?: string): DisplayPrefs {
  let base: DisplayPrefs;
  try {
    const raw = localStorage.getItem(LS_KEY);
    base = raw ? sanitize(JSON.parse(raw)) : { ...DEFAULT_PREFS };
  } catch {
    base = { ...DEFAULT_PREFS };
  }
  // Per-session font override — typing in claude is often heavy on
  // code (small font) while reading docs prefers a larger one; letting
  // each session remember its own scale removes the friction of
  // re-adjusting when switching back and forth.
  if (session) {
    try {
      const sz = parseInt(localStorage.getItem(`ccpipe.fontSize.${session}`) ?? "", 10);
      if (Number.isFinite(sz) && sz >= 8 && sz <= 32) base.fontSize = sz;
    } catch {}
  }
  return base;
}

export function saveSessionFontSize(session: string, fontSize: number): void {
  if (!session) return;
  try {
    localStorage.setItem(`ccpipe.fontSize.${session}`, String(Math.round(fontSize)));
  } catch {}
}

export function saveDisplayPrefs(prefs: DisplayPrefs): DisplayPrefs {
  const clean = sanitize(prefs);
  localStorage.setItem(LS_KEY, JSON.stringify(clean));
  return clean;
}

// Subscribe to changes from other tabs (storage event). Returns an
// unsubscribe function.
export function onDisplayPrefsChange(cb: (prefs: DisplayPrefs) => void): () => void {
  const handler = (e: StorageEvent) => {
    if (e.key !== LS_KEY) return;
    cb(loadDisplayPrefs());
  };
  window.addEventListener("storage", handler);
  return () => window.removeEventListener("storage", handler);
}

// ─── Last attached session ─────────────────────────────────────────────
// Stored so a page refresh on a flaky mobile connection lands the user
// straight back in the session they were in.

const LAST_SESSION_KEY = "ccpipe.lastSession";

export function loadLastSession(): string | null {
  try {
    const v = localStorage.getItem(LAST_SESSION_KEY);
    return v && typeof v === "string" ? v : null;
  } catch { return null; }
}

export function saveLastSession(name: string): void {
  try { localStorage.setItem(LAST_SESSION_KEY, name); } catch {}
}

export function clearLastSession(): void {
  try { localStorage.removeItem(LAST_SESSION_KEY); } catch {}
}

// ─── TTS mute (per-tab affordance, but persisted) ──────────────────────

const TTS_MUTE_KEY = "ccpipe.tts.muted";

function _sessionMuteKey(session: string): string {
  return `${TTS_MUTE_KEY}.${session}`;
}

export function loadTtsMuted(session?: string): boolean {
  try {
    if (session) {
      // Per-session override. Empty string ≠ stored; only "0"/"1" count.
      const v = localStorage.getItem(_sessionMuteKey(session));
      if (v === "0") return false;
      if (v === "1") return true;
    }
    return localStorage.getItem(TTS_MUTE_KEY) === "1";
  } catch { return false; }
}

export function saveTtsMuted(muted: boolean, session?: string): void {
  try {
    if (session) localStorage.setItem(_sessionMuteKey(session), muted ? "1" : "0");
    else localStorage.setItem(TTS_MUTE_KEY, muted ? "1" : "0");
  } catch {}
}

// ─── Per-session scroll position ───────────────────────────────────────
// We save how far from the bottom (in pixels) the user has scrolled, so
// that a refresh keeps them roughly where they were reading rather than
// snapping to the live tail. Keyed by tmux session name.

const SCROLL_KEY = "ccpipe.scroll";

interface ScrollMap { [session: string]: number }

// In-memory cache of the parsed map. Avoids re-parsing localStorage on
// every save (the debounce in terminal.ts cuts the call frequency, but
// caching makes loads + writes O(1) for the common path).
//
// Invalidated when another tab writes (storage event below), so a
// second window scrolling its session's offset is still respected.
let _scrollMapCache: ScrollMap | null = null;

function _readScrollMap(): ScrollMap {
  if (_scrollMapCache) return _scrollMapCache;
  try {
    const raw = localStorage.getItem(SCROLL_KEY);
    if (!raw) { _scrollMapCache = {}; return _scrollMapCache; }
    const obj = JSON.parse(raw);
    _scrollMapCache = obj && typeof obj === "object" ? obj : {};
    return _scrollMapCache!;
  } catch {
    _scrollMapCache = {};
    return _scrollMapCache;
  }
}

if (typeof window !== "undefined") {
  window.addEventListener("storage", (e) => {
    if (e.key === SCROLL_KEY) _scrollMapCache = null;
  });
}

export function loadScrollOffset(session: string): number | null {
  const map = _readScrollMap();
  const v = map[session];
  // Number.isFinite rejects Infinity / -Infinity / NaN that bare
  // typeof === "number" + comparison would otherwise allow through
  // from a hand-edited localStorage value.
  return typeof v === "number" && Number.isFinite(v) && v >= 0 ? v : null;
}

export function saveScrollOffset(session: string, offsetFromBottom: number): void {
  try {
    const map = _readScrollMap();
    map[session] = Math.max(0, Math.round(offsetFromBottom));
    localStorage.setItem(SCROLL_KEY, JSON.stringify(map));
  } catch {}
}

export function clearScrollOffset(session: string): void {
  try {
    const map = _readScrollMap();
    delete map[session];
    localStorage.setItem(SCROLL_KEY, JSON.stringify(map));
  } catch {}
}
