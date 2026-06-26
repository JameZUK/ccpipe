// Reproduce the reconnect "duplicate scrollback" seam deterministically.
//
// On a full (non-seamless) reconnect the backend replays a capture-pane
// blob, then tmux's attach redraw repaints the visible screen. If the
// replayed blob ends with a trailing CRLF, writing it scrolls the grid up
// by one row (the final CRLF pushes the top visible line into scrollback);
// the subsequent in-place attach redraw then repaints that same line on
// screen — so it appears BOTH in scrollback and visible = a duplicate,
// one per reconnect. This script proves it and proves that dropping the
// trailing CRLF removes the duplicate.

import pkg from "@xterm/headless";
const { Terminal } = pkg;

const ROWS = 44, COLS = 60;

function makeTerm() {
  return new Terminal({ cols: COLS, rows: ROWS, scrollback: 10000, allowProposedApi: true });
}
function writeAsync(term, data) {
  return new Promise((r) => term.write(data, r));
}
function dump(term) {
  const buf = term.buffer.active;
  const out = [];
  for (let i = 0; i < buf.length; i++) {
    const l = buf.getLine(i);
    out.push(l ? l.translateToString(true).replace(/\s+$/u, "") : "");
  }
  return out;
}
// Count lines that appear more than once (the duplication signal).
function dupCount(lines) {
  const seen = new Map();
  for (const l of lines) if (l) seen.set(l, (seen.get(l) || 0) + 1);
  let dups = 0;
  for (const [, n] of seen) if (n > 1) dups += n - 1;
  return dups;
}

// A "visible screen" = ROWS distinct content lines (like claude's TUI).
const screen = Array.from({ length: ROWS }, (_, i) => `LINE ${String(i).padStart(2, "0")} content`);

// Backend capture blob: every line CRLF-terminated. `trailing` controls
// whether the LAST line also gets a CRLF (current behaviour = true).
function captureBlob(trailing) {
  let s = screen.map((l) => l + "\r\n").join("");
  if (!trailing) s = s.replace(/\r\n$/, "");
  return s;
}

// tmux attach redraw: home, then repaint each row in place via absolute
// cursor positioning + clear-to-EOL. No scrolling — overwrites the grid.
function attachRedraw() {
  let s = "\x1b[H";
  for (let r = 0; r < ROWS; r++) s += `\x1b[${r + 1};1H` + screen[r] + "\x1b[K";
  return s;
}

async function run(trailing) {
  const term = makeTerm();
  // 1. reset (full reconnect wipes), 2. replay capture, 3. attach redraw
  await writeAsync(term, "\x1b[2J\x1b[H");
  await writeAsync(term, captureBlob(trailing));
  await writeAsync(term, attachRedraw());
  const lines = dump(term);
  return { dups: dupCount(lines), bufferLen: term.buffer.active.length };
}

const withTrailing = await run(true);
const without = await run(false);
console.log(`current (trailing CRLF):  duplicate lines = ${withTrailing.dups}  bufferLen = ${withTrailing.bufferLen}`);
console.log(`fixed   (no trailing CRLF): duplicate lines = ${without.dups}  bufferLen = ${without.bufferLen}`);
console.log(without.dups === 0 && withTrailing.dups > 0
  ? "✅ trailing CRLF causes the dup; dropping it fixes it"
  : "⚠ result not as predicted — investigate further");
