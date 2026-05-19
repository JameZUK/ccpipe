// Cursor-style + colour catalogue for the terminal.
//
// xterm.js's cursor surface is small (style, blink, width, inactive-
// style, theme.cursor / theme.cursorAccent), so this file collects
// the curated picker options + resolver helpers in one place. The
// settings UI uses these to populate dropdowns; terminal.ts uses
// them when applying prefs to the live xterm.

/** Cursor styles xterm renders, both active and inactive. */
export type CursorStyle = "bar" | "block" | "underline";

/** xterm 5+ also supports `outline` (hollow block) and `none`
 * specifically for the inactive cursor. Listed separately so the
 * active-cursor dropdown stays clean. */
export type CursorInactiveStyle =
  | "bar"
  | "block"
  | "underline"
  | "outline"
  | "none";


export interface CursorColor {
  /** Stable id persisted to localStorage. */
  id: string;
  /** Shown in the settings UI. */
  label: string;
  /** CSS colour value handed to xterm's theme.cursor. */
  value: string;
}

/** Curated palette. The default `amber` matches the rest of the
 * ccpipe chrome. Other entries are common terminal palette colours
 * with enough chroma to read against the charcoal background. */
export const CURSOR_COLORS: readonly CursorColor[] = [
  { id: "amber",     label: "Amber",     value: "#f5a524" },
  { id: "parchment", label: "Parchment", value: "#e8dfc8" },
  { id: "green",     label: "Green",     value: "#7d9e60" },
  { id: "cyan",      label: "Cyan",      value: "#4ec9a8" },
  { id: "pink",      label: "Pink",      value: "#c586c0" },
  { id: "red",       label: "Red",       value: "#e95141" },
] as const;


export function isKnownCursorColor(id: string | undefined | null): boolean {
  if (!id) return false;
  return CURSOR_COLORS.some(c => c.id === id);
}

export function isKnownCursorInactiveStyle(s: string | undefined | null): boolean {
  return s === "bar" || s === "block" || s === "underline"
      || s === "outline" || s === "none";
}

export function resolveCursorColor(id: string | undefined | null): string {
  const found = CURSOR_COLORS.find(c => c.id === id);
  return found?.value ?? CURSOR_COLORS[0].value;
}
