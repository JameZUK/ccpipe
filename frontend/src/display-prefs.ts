// Per-device display preferences for the terminal. Stored in
// localStorage so each browser/device has its own — these aren't worth
// syncing through the server.

import {
  CursorInactiveStyle,
  CursorStyle,
  isKnownCursorColor,
  isKnownCursorInactiveStyle,
} from "./terminal-cursor";
import { isKnownFontId } from "./terminal-fonts";

const LS_KEY = "ccpipe.display.v1";

export type { CursorStyle, CursorInactiveStyle };

export interface DisplayPrefs {
  fontSize: number;        // 8..22 px
  lineHeight: number;      // 1.0..1.6
  letterSpacing: number;   // 0..3 px
  cursorStyle: CursorStyle;
  cursorBlink: boolean;
  // Inactive-cursor rendering (when the terminal element loses
  // focus). xterm 5+ supports the "outline" hollow-block + "none"
  // hide variants in addition to the bar/block/underline pair.
  cursorInactiveStyle: CursorInactiveStyle;
  // Stable id from CURSOR_COLORS (terminal-cursor.ts); resolved to
  // a CSS colour by resolveCursorColor() before being handed to
  // xterm's theme.cursor.
  cursorColor: string;
  // Bar-cursor thickness in pixels. xterm clamps higher values down
  // to its cell width; 1-3 is the useful visible range.
  cursorWidth: number;
  // Terminal font is split desktop / mobile because what reads well
  // on a 27" screen is rarely what reads well on a phone. The
  // resolver in `terminal-fonts.ts` maps these ids onto the CSS
  // font-family string xterm receives, and the same ids drive the
  // two Settings → Display dropdowns. Defaults below pick a sane
  // starting point per device class without forcing a download
  // (system mono) on desktop, while preferring a legibility-tuned
  // bundled font on mobile.
  terminalFontDesktop: string;
  terminalFontMobile: string;
}

export const DEFAULT_PREFS: DisplayPrefs = {
  fontSize: 14,
  lineHeight: 1.15,
  letterSpacing: 0,
  cursorStyle: "bar",
  cursorBlink: true,
  cursorInactiveStyle: "outline",   // distinct from focused — easy to read
  cursorColor: "amber",
  cursorWidth: 1,
  terminalFontDesktop: "system",
  terminalFontMobile: "jetbrains-mono",
};

// Canonical terminal font-size bounds. Used by BOTH the global clamp in
// sanitize() and the per-session override below so a stored session
// size can't fall outside the range the Settings slider can represent
// (the two previously disagreed: 8..22 vs 8..32, so a stored 23-32
// desynced from the slider).
export const FONT_SIZE_MIN = 8;
export const FONT_SIZE_MAX = 22;

function clamp(n: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, n));
}

function sanitize(p: Partial<DisplayPrefs>): DisplayPrefs {
  return {
    fontSize: clamp(Math.round(p.fontSize ?? DEFAULT_PREFS.fontSize), FONT_SIZE_MIN, FONT_SIZE_MAX),
    lineHeight: clamp(Number(p.lineHeight ?? DEFAULT_PREFS.lineHeight), 1.0, 1.6),
    letterSpacing: clamp(Number(p.letterSpacing ?? DEFAULT_PREFS.letterSpacing), 0, 3),
    cursorStyle: (["bar", "block", "underline"] as const).includes(p.cursorStyle as CursorStyle)
      ? (p.cursorStyle as CursorStyle)
      : DEFAULT_PREFS.cursorStyle,
    cursorBlink: typeof p.cursorBlink === "boolean" ? p.cursorBlink : DEFAULT_PREFS.cursorBlink,
    cursorInactiveStyle: isKnownCursorInactiveStyle(p.cursorInactiveStyle as string)
      ? (p.cursorInactiveStyle as CursorInactiveStyle)
      : DEFAULT_PREFS.cursorInactiveStyle,
    cursorColor: isKnownCursorColor(p.cursorColor as string)
      ? (p.cursorColor as string)
      : DEFAULT_PREFS.cursorColor,
    cursorWidth: clamp(
      Math.round(Number(p.cursorWidth ?? DEFAULT_PREFS.cursorWidth)), 1, 3),
    // Validate against the known font catalogue — an unknown id (e.g.
    // a font we removed in a later version) falls back to the
    // device-appropriate default rather than blanking the terminal.
    terminalFontDesktop: isKnownFontId(p.terminalFontDesktop as string)
      ? (p.terminalFontDesktop as string)
      : DEFAULT_PREFS.terminalFontDesktop,
    terminalFontMobile: isKnownFontId(p.terminalFontMobile as string)
      ? (p.terminalFontMobile as string)
      : DEFAULT_PREFS.terminalFontMobile,
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
      if (Number.isFinite(sz) && sz >= FONT_SIZE_MIN && sz <= FONT_SIZE_MAX) base.fontSize = sz;
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

// (Removed) Per-session scroll-position persistence used to live here.
// It was a "land the user back where they were reading" feature that
// in practice yanked operators out of the live tail on every refresh,
// so the consumer was removed in 13b3704 and the producer follows
// here. If you want it back, design the UX so the user has to OPT IN
// (e.g. only when they explicitly bookmarked a scroll position)
// rather than silently doing it on every attach.
