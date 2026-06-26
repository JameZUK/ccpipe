// DEFINITIVE headless reproduction of the "duplicate scrollback on
// SEAMLESS reconnect" bug.
//
// Model: a SEAMLESS reconnect preserves the xterm buffer (NO reset) and
// then lets the live stream through — including tmux's attach redraw and
// claude's (Ink) re-render triggered by the reattach/SIGWINCH. The
// question this script answers deterministically: does that re-emission
// of the VISIBLE frame push the already-present visible lines into
// scrollback (duplicating them), and which redraw SHAPE does it?
//
// It replicates production (frontend/src/terminal.ts) faithfully:
//   - the InputHandler.scrollUp (CSI SU) patch that PUSHES the scrolled
//     -out top line of a top=0 scroll region into scrollback via
//     bufferService.scroll() (the upstream handler silently discards it);
//   - alt-screen (?1049h/47/1047/1048) suppression so output stays on
//     the MAIN buffer;
//   - ED3 (\x1b[3J / \x1b[?3J) scrollback-wipe suppression.
//
// Three redraw shapes are compared, each WITHOUT a reset (= seamless):
//   A. CUP-absolute : repaint every visible row in place with absolute
//                     cursor positioning + clear-to-EOL. No scrolling.
//   B. SU-scroll    : scroll the whole screen up via CSI S (SU) with a
//                     top=0 scroll region, then repaint. Hits the patch.
//   C. LF-scroll    : park the cursor at the bottom row and emit one LF
//                     per row (xterm's native scroll), then repaint.
//
// Usage: node test/dup-repro.mjs

import pkg from "@xterm/headless";
const { Terminal } = pkg;

const ROWS = 44, COLS = 60;
const INITIAL_LINES = 80;   // > ROWS so some content starts in scrollback

function writeAsync(term, data) {
  return new Promise((r) => term.write(data, r));
}

// Build a Terminal wired exactly like production createTerminal():
// alt-screen suppression, ED3 suppression, and the scrollUp (SU) patch.
function makeTerm() {
  const term = new Terminal({
    cols: COLS, rows: ROWS, scrollback: 10000,
    allowProposedApi: true, convertEol: false,
  });

  // ── alt-screen suppression (terminal.ts) ──
  const ALT = new Set([47, 1047, 1048, 1049]);
  const suppressAlt = (params) => {
    const first = Array.isArray(params[0]) ? params[0][0] : params[0];
    return typeof first === "number" && ALT.has(first);
  };
  term.parser.registerCsiHandler({ prefix: "?", final: "h" }, suppressAlt);
  term.parser.registerCsiHandler({ prefix: "?", final: "l" }, suppressAlt);

  // ── ED3 scrollback-wipe suppression (terminal.ts) ──
  const firstParam = (params) => {
    const p = params[0];
    const v = Array.isArray(p) ? p[0] : p;
    return typeof v === "number" ? v : 0;
  };
  term.parser.registerCsiHandler({ final: "J" }, (p) => firstParam(p) === 3);
  term.parser.registerCsiHandler({ prefix: "?", final: "J" }, (p) => firstParam(p) === 3);

  // ── CSI SU (scrollUp) patch (terminal.ts) — push scrolled-out top line
  //    of a top=0 scroll region into scrollback. ──
  let suPatchInstalled = false;
  const core = term._core;
  const ih = core?._inputHandler;
  const bs = core?._bufferService;
  if (
    ih && bs &&
    typeof ih.scrollUp === "function" &&
    typeof bs.scroll === "function" &&
    typeof ih._eraseAttrData === "function" &&
    bs.buffer
  ) {
    suPatchInstalled = true;
    const originalScrollUp = ih.scrollUp.bind(ih);
    ih.scrollUp = function (params) {
      const buf = bs.buffer;
      if (buf.scrollTop !== 0) return originalScrollUp(params);
      let count = Math.min((params.params?.[0]) || 1, term.rows);
      while (count-- > 0) bs.scroll(ih._eraseAttrData());
      ih._dirtyRowTracker?.markRangeDirty?.(buf.scrollTop, buf.scrollBottom);
      return true;
    };
  }
  if (!suPatchInstalled) throw new Error("SU patch failed to install — internals moved");
  return term;
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

// Count surplus copies of any non-blank line (the duplication signal).
function dupCount(lines) {
  const seen = new Map();
  for (const l of lines) if (l) seen.set(l, (seen.get(l) || 0) + 1);
  let dups = 0;
  for (const [, n] of seen) if (n > 1) dups += n - 1;
  return dups;
}

// Read the CURRENT visible rows (baseY .. baseY+rows-1) as strings.
function visibleRows(term) {
  const buf = term.buffer.active;
  const out = [];
  for (let r = 0; r < term.rows; r++) {
    const l = buf.getLine(buf.baseY + r);
    out.push(l ? l.translateToString(true).replace(/\s+$/u, "") : "");
  }
  return out;
}

// Lay down INITIAL_LINES distinct numbered lines; the last ROWS stay
// visible, the rest land in scrollback (native xterm LF scroll).
async function seedInitial(term) {
  let s = "";
  for (let i = 0; i < INITIAL_LINES; i++) {
    s += `LINE ${String(i).padStart(3, "0")} content`;
    if (i < INITIAL_LINES - 1) s += "\r\n";
  }
  await writeAsync(term, s);
}

// ── Redraw shape A: absolute repaint, no scroll. ──
function redrawCUP(rows) {
  let s = "\x1b[H";
  for (let r = 0; r < rows.length; r++) {
    s += `\x1b[${r + 1};1H` + rows[r] + "\x1b[K";
  }
  return s;
}

// ── Redraw shape B: SU-scroll the whole screen out, then repaint. ──
// Set a top=0 scroll region (DECSTBM 1;ROWS), scroll up by ROWS via
// CSI S, then absolute-repaint the identical frame.
function redrawSU(rows) {
  let s = `\x1b[1;${ROWS}r`;     // DECSTBM, top=1 => scrollTop=0
  s += `\x1b[${rows.length}S`;   // SU by N rows -> patch pushes N lines to scrollback
  s += redrawCUP(rows);
  s += "\x1b[r";                 // reset scroll region
  return s;
}

// ── Redraw shape C: park cursor at bottom, emit one LF per row
//    (xterm native scroll), then repaint. ──
function redrawLF(rows) {
  let s = `\x1b[${ROWS};1H`;     // cursor to bottom-left
  s += "\n".repeat(rows.length); // native LF scroll -> native scrollback push
  s += redrawCUP(rows);
  return s;
}

async function run(name, buildRedraw) {
  const term = makeTerm();
  await seedInitial(term);
  const before = term.buffer.active.length;
  const dupsBefore = dupCount(dump(term));
  const frame = visibleRows(term);          // exactly what gets re-emitted
  // SEAMLESS reconnect: NO reset; live stream (attach redraw) resumes
  // over the existing buffer.
  await writeAsync(term, buildRedraw(frame));
  const after = term.buffer.active.length;
  const dupsAfter = dupCount(dump(term));
  return { name, before, after, grew: after - before, dupsBefore, dupsAfter };
}

const shapes = [
  ["A CUP-absolute", redrawCUP],
  ["B SU-scroll    ", redrawSU],
  ["C LF-scroll    ", redrawLF],
];

console.log(`seed=${INITIAL_LINES} lines @ ${COLS}x${ROWS}, seamless reconnect (no reset)\n`);
console.log("shape           bufLen before->after  grew  dupsBefore  dupsAfter");
const results = [];
for (const [name, fn] of shapes) {
  const r = await run(name, fn);
  results.push(r);
  console.log(
    `${r.name}   ${String(r.before).padStart(4)} -> ${String(r.after).padStart(4)}` +
    `      ${String(r.grew).padStart(3)}      ${String(r.dupsBefore).padStart(5)}      ${String(r.dupsAfter).padStart(5)}`,
  );
}

console.log("");
for (const r of results) {
  const delta = r.dupsAfter - r.dupsBefore;
  if (delta > 0) {
    console.log(`>> ${r.name.trim()} DUPLICATES: +${delta} dup lines pushed into scrollback by the re-emitted frame`);
  } else {
    console.log(`   ${r.name.trim()} clean: no new duplicates`);
  }
}
