// Catalogue of terminal fonts bundled with ccpipe.
//
// Each font entry corresponds to a `.woff2` file in
// `frontend/src/assets/fonts/` and a matching `@font-face` declaration
// in `styles.css`. The src/ location is intentional — Vite resolves
// CSS-referenced assets there cleanly (hashes them, emits to
// dist/assets/), whereas public/ + an HTML <link> caused weird
// path-preservation in the build output. Adding a font is a
// three-step process:
//
//   1. Drop the .woff2 in `frontend/src/assets/fonts/<id>.woff2`
//   2. Add a `@font-face` block in styles.css that references it
//      via `url('./assets/fonts/<id>.woff2')`
//   3. Add a row to `TERMINAL_FONTS` below
//
// Fonts are loaded lazily by the browser — declaring an @font-face
// does not trigger a download; the woff2 only fetches when CSS
// actually applies that font-family to rendered text. So this list
// can grow without affecting first-paint cost for users who stick
// with the default.

/** System-mono fallback stack. Used by the "system" choice AND as
 * the fallback chain after each custom font name, so xterm still
 * has glyphs to render during the brief `font-display: swap` window
 * before the custom font's woff2 finishes loading. */
const SYSTEM_STACK =
  'ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace';


export interface TerminalFont {
  /** Stable identifier persisted to localStorage. Don't rename
   * without writing a migration in display-prefs.ts. */
  id: string;
  /** Shown in the Settings → Display dropdown. */
  label: string;
  /** CSS font-family value the @font-face block declares. Undefined
   * for the "system" entry (which uses the fallback stack only). */
  family?: string;
  /** Optional one-line description shown as a hint under the dropdown
   * when this font is selected. Useful for nudging mobile users
   * toward the legibility-tuned options. */
  hint?: string;
  /** Marks fonts particularly well-suited to small mobile screens —
   * surfaced in the dropdown ordering / hint copy for the mobile
   * selector. */
  mobileFriendly?: boolean;
}


export const TERMINAL_FONTS: readonly TerminalFont[] = [
  // System default — no download, OS native rendering.
  { id: "system", label: "System mono",
    hint: "OS native — SF Mono / Menlo on macOS, Consolas on Windows, etc." },

  // Mobile-clarity picks.
  { id: "jetbrains-mono", label: "JetBrains Mono", family: "JetBrains Mono",
    hint: "Wide characters, ligatures, holds up below 12px",
    mobileFriendly: true },
  { id: "roboto-mono", label: "Roboto Mono", family: "Roboto Mono",
    hint: "Google's mono — designed specifically for small-screen Android",
    mobileFriendly: true },
  { id: "atkinson-hyperlegible-mono", label: "Atkinson Hyperlegible Mono",
    family: "Atkinson Hyperlegible Mono",
    hint: "Designed by the Braille Institute for low-vision accessibility",
    mobileFriendly: true },
  { id: "cousine", label: "Cousine", family: "Cousine",
    hint: "Chrome OS terminal default — tuned for compact UI",
    mobileFriendly: true },
  { id: "hack", label: "Hack", family: "Hack",
    hint: "Purpose-built for source code — no frills, all clarity",
    mobileFriendly: true },

  // Modern coding fonts.
  { id: "fira-code", label: "Fira Code", family: "Fira Code",
    hint: "The original ligature font; Mozilla heritage" },
  { id: "cascadia-code", label: "Cascadia Code", family: "Cascadia Code",
    hint: "Microsoft's, sharp ligatures, designed for Windows Terminal" },
  { id: "geist-mono", label: "Geist Mono", family: "Geist Mono",
    hint: "Vercel's — geometric, distinctive zero/one, very modern" },
  { id: "iosevka", label: "Iosevka", family: "Iosevka",
    hint: "Narrow + sharp; more characters per row for wide terminals" },

  // Clean classics.
  { id: "ibm-plex-mono", label: "IBM Plex Mono", family: "IBM Plex Mono",
    hint: "Calm, corporate-clean — reads evenly at all sizes" },
  { id: "source-code-pro", label: "Source Code Pro", family: "Source Code Pro",
    hint: "Adobe's, slightly condensed, neutral" },
  { id: "dm-mono", label: "DM Mono", family: "DM Mono",
    hint: "Geometric, humanist — the font used by the rest of ccpipe's UI" },
] as const;


export function isKnownFontId(id: string | undefined | null): boolean {
  if (!id) return false;
  return TERMINAL_FONTS.some(f => f.id === id);
}


/** Resolve a font id into the CSS `font-family` string xterm should
 * receive. Returns the bare system stack for "system" or any unknown
 * id; for a known custom font, returns `'Font Name', <system stack>`
 * so xterm has a fallback during the woff2 load window. */
export function resolveTerminalFontFamily(id: string | undefined | null): string {
  if (!id) return SYSTEM_STACK;
  const font = TERMINAL_FONTS.find(f => f.id === id);
  if (!font || !font.family) return SYSTEM_STACK;
  return `'${font.family}', ${SYSTEM_STACK}`;
}
