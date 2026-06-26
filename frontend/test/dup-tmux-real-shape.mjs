// Adversarial test: does the REAL captured tmux on-attach redraw shape
// duplicate scrollback over a preserved (seamless-reconnect) buffer?
//
// Prior phase captured tmux 3.6b on-attach bytes (socket wf_dup_8421):
//   per repaint pass: ?1049h (suppressed) -> CSI H -> CSI J (ED0, clears
//   visible grid IN PLACE, no scroll) -> DECSTBM 1;rows -> CSI H -> for
//   EVERY row: <text> + CSI K (EL) + CR LF.  ZERO CSI S. ED0 only.
//
// Shape C in dup-repro.mjs scrolls the OLD visible frame fully into
// scrollback (N LFs at the bottom) BEFORE repainting -> N dups. Real
// tmux clears IN PLACE first (ED0, no scroll) then repaints. This script
// pits the two shapes head-to-head on an identical preserved buffer,
// wired exactly like terminal.ts (SU patch + alt/ED3 suppression).

import pkg from "@xterm/headless";
const { Terminal } = pkg;

const ROWS = 44, COLS = 60, INITIAL_LINES = 80;

function writeAsync(term, data) { return new Promise((r) => term.write(data, r)); }

function makeTerm() {
  const term = new Terminal({ cols: COLS, rows: ROWS, scrollback: 10000,
    allowProposedApi: true, convertEol: false });
  const ALT = new Set([47, 1047, 1048, 1049]);
  const suppressAlt = (p) => { const f = Array.isArray(p[0]) ? p[0][0] : p[0];
    return typeof f === "number" && ALT.has(f); };
  term.parser.registerCsiHandler({ prefix: "?", final: "h" }, suppressAlt);
  term.parser.registerCsiHandler({ prefix: "?", final: "l" }, suppressAlt);
  const fp = (p) => { const v = Array.isArray(p[0]) ? p[0][0] : p[0];
    return typeof v === "number" ? v : 0; };
  term.parser.registerCsiHandler({ final: "J" }, (p) => fp(p) === 3);
  term.parser.registerCsiHandler({ prefix: "?", final: "J" }, (p) => fp(p) === 3);
  const core = term._core, ih = core?._inputHandler, bs = core?._bufferService;
  if (ih && bs && typeof ih.scrollUp === "function" && typeof bs.scroll === "function"
      && typeof ih._eraseAttrData === "function" && bs.buffer) {
    const orig = ih.scrollUp.bind(ih);
    ih.scrollUp = function (params) {
      const buf = bs.buffer;
      if (buf.scrollTop !== 0) return orig(params);
      let count = Math.min((params.params?.[0]) || 1, term.rows);
      while (count-- > 0) bs.scroll(ih._eraseAttrData());
      ih._dirtyRowTracker?.markRangeDirty?.(buf.scrollTop, buf.scrollBottom);
      return true;
    };
  } else throw new Error("SU patch failed to install");
  return term;
}

function dump(term) {
  const buf = term.buffer.active, out = [];
  for (let i = 0; i < buf.length; i++) {
    const l = buf.getLine(i);
    out.push(l ? l.translateToString(true).replace(/\s+$/u, "") : "");
  }
  return out;
}
function dupCount(lines) {
  const seen = new Map();
  for (const l of lines) if (l) seen.set(l, (seen.get(l) || 0) + 1);
  let d = 0; for (const [, n] of seen) if (n > 1) d += n - 1; return d;
}
function visibleRows(term) {
  const buf = term.buffer.active, out = [];
  for (let r = 0; r < term.rows; r++) {
    const l = buf.getLine(buf.baseY + r);
    out.push(l ? l.translateToString(true).replace(/\s+$/u, "") : "");
  }
  return out;
}
async function seedInitial(term) {
  let s = "";
  for (let i = 0; i < INITIAL_LINES; i++) {
    s += `LINE ${String(i).padStart(3, "0")} content`;
    if (i < INITIAL_LINES - 1) s += "\r\n";
  }
  await writeAsync(term, s);
}

// REAL tmux on-attach redraw shape: alt-enter (suppressed) -> home ->
// ED0 (in-place clear, NO scroll) -> set scroll region -> home ->
// per row: text + EL + CRLF.  withTrailingLF toggles whether the LAST
// row also gets a CRLF (the only LF that could hit the region bottom).
function redrawTmuxReal(rows, withTrailingLF) {
  let s = "\x1b[?1049h";          // alt enter (frontend suppresses)
  s += "\x1b[H";                  // home
  s += "\x1b[J";                  // ED0 - erase cursor..end, in place
  s += `\x1b[1;${ROWS}r`;         // DECSTBM 1;ROWS
  s += "\x1b[H";                  // home again
  for (let r = 0; r < rows.length; r++) {
    s += rows[r] + "\x1b[K";      // text + EL
    if (r < rows.length - 1 || withTrailingLF) s += "\r\n";
  }
  s += "\x1b[r";                  // reset region
  s += "\x1b[?1049l";             // alt leave (suppressed)
  return s;
}

async function run(name, build) {
  const term = makeTerm();
  await seedInitial(term);
  const before = term.buffer.active.length;
  const dupsBefore = dupCount(dump(term));
  const frame = visibleRows(term);
  await writeAsync(term, build(frame));      // SEAMLESS: no reset
  const after = term.buffer.active.length;
  const dupsAfter = dupCount(dump(term));
  return { name, before, after, grew: after - before, dupsBefore, dupsAfter };
}

const cases = [
  ["tmux-real (no trailing LF)", (f) => redrawTmuxReal(f, false)],
  ["tmux-real (trailing LF)   ", (f) => redrawTmuxReal(f, true)],
];

console.log(`seed=${INITIAL_LINES} @ ${COLS}x${ROWS}, seamless (no reset)\n`);
console.log("case                          bufLen  grew  dupsBefore  dupsAfter");
for (const [name, fn] of cases) {
  const r = await run(name, fn);
  console.log(`${r.name}   ${String(r.before).padStart(3)}->${String(r.after).padStart(3)}` +
    `   ${String(r.grew).padStart(3)}     ${String(r.dupsBefore).padStart(4)}     ${String(r.dupsAfter).padStart(4)}` +
    `   ${r.dupsAfter - r.dupsBefore > 0 ? "DUP +" + (r.dupsAfter - r.dupsBefore) : "clean"}`);
}
