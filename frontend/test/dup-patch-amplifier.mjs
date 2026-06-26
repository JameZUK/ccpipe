// Adversarial verification of CANDIDATE [scrollup-patch-amplifier]:
//   "the patched InputHandler.scrollUp is the amplifier that converts a
//    reconnect redraw into duplicated scrollback".
//
// This extends dup-repro.mjs to toggle the SU patch ON/OFF and to model
// the REAL tmux-3.6b attach redraw shape captured on socket wf_dup_8421:
//   per repaint pass: CSI?1049h (suppressed) -> CSI H + CSI J(ED0)
//   -> DECSTBM 1;rows -> CSI H(home) -> for every row: <text> + CSI K(EL)
//   + CR + LF.  ZERO CSI S.  The scrolling primitive is the trailing LF at
//   the bottom row of the active scroll region.
//
// Decisive question: if the SU patch is the amplifier, disabling it should
// drop the dup count to 0 for the redraw shape tmux ACTUALLY emits. If the
// tmux-attach (LF) shape still duplicates with the patch OFF, the patch is
// NOT the mechanism for the real-world trigger.
//
// Usage: node test/dup-patch-amplifier.mjs

import pkg from "@xterm/headless";
const { Terminal } = pkg;

const ROWS = 44, COLS = 60;
const INITIAL_LINES = 80;

const writeAsync = (term, data) => new Promise((r) => term.write(data, r));

function makeTerm(installPatch) {
  const term = new Terminal({
    cols: COLS, rows: ROWS, scrollback: 10000,
    allowProposedApi: true, convertEol: false,
  });
  const ALT = new Set([47, 1047, 1048, 1049]);
  const suppressAlt = (params) => {
    const first = Array.isArray(params[0]) ? params[0][0] : params[0];
    return typeof first === "number" && ALT.has(first);
  };
  term.parser.registerCsiHandler({ prefix: "?", final: "h" }, suppressAlt);
  term.parser.registerCsiHandler({ prefix: "?", final: "l" }, suppressAlt);
  const firstParam = (params) => {
    const p = params[0];
    const v = Array.isArray(p) ? p[0] : p;
    return typeof v === "number" ? v : 0;
  };
  // ED3 suppression only (ED0/1/2 fall through, matching terminal.ts).
  term.parser.registerCsiHandler({ final: "J" }, (p) => firstParam(p) === 3);
  term.parser.registerCsiHandler({ prefix: "?", final: "J" }, (p) => firstParam(p) === 3);

  if (installPatch) {
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
      const originalScrollUp = ih.scrollUp.bind(ih);
      ih.scrollUp = function (params) {
        const buf = bs.buffer;
        if (buf.scrollTop !== 0) return originalScrollUp(params);
        let count = Math.min((params.params?.[0]) || 1, term.rows);
        while (count-- > 0) bs.scroll(ih._eraseAttrData());
        ih._dirtyRowTracker?.markRangeDirty?.(buf.scrollTop, buf.scrollBottom);
        return true;
      };
    } else {
      throw new Error("SU patch failed to install — internals moved");
    }
  }
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
function dupCount(lines) {
  const seen = new Map();
  for (const l of lines) if (l) seen.set(l, (seen.get(l) || 0) + 1);
  let dups = 0;
  for (const [, n] of seen) if (n > 1) dups += n - 1;
  return dups;
}
function visibleRows(term) {
  const buf = term.buffer.active;
  const out = [];
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

function redrawCUP(rows) {
  let s = "\x1b[H";
  for (let r = 0; r < rows.length; r++) s += `\x1b[${r + 1};1H` + rows[r] + "\x1b[K";
  return s;
}
function redrawSU(rows) {
  let s = `\x1b[1;${ROWS}r`;
  s += `\x1b[${rows.length}S`;
  s += redrawCUP(rows);
  s += "\x1b[r";
  return s;
}
function redrawLF(rows) {
  let s = `\x1b[${ROWS};1H`;
  s += "\n".repeat(rows.length);
  s += redrawCUP(rows);
  return s;
}
// Shape D: the REAL tmux-3.6b on-attach redraw, byte-faithful to the
// wf_dup_8421 capture. ?1049h (suppressed) + CSI H + ED0 + DECSTBM full
// screen + home, then per row: text + EL + CRLF. The CRLF after the
// bottom row (and the way the home-anchored top-to-bottom fill advances
// past scrollBottom) is the scroll primitive — no CSI S anywhere.
function redrawTmuxAttach(rows) {
  let s = "\x1b[?1049h";          // alt-screen enter (suppressed by handler)
  s += "\x1b[H";                  // home
  s += "\x1b[J";                  // ED0 erase cursor->end (does NOT scroll/wipe scrollback)
  s += `\x1b[1;${ROWS}r`;         // DECSTBM full-screen scroll region (scrollTop=0)
  s += "\x1b[H";                  // home again
  for (let r = 0; r < rows.length; r++) {
    s += rows[r] + "\x1b[K";      // row text + EL
    s += "\r\n";                  // CR + LF after EVERY row (incl. bottom)
  }
  s += "\x1b[r";                  // reset scroll region (tmux emits CSI 1;0r)
  return s;
}

async function run(name, buildRedraw, installPatch) {
  const term = makeTerm(installPatch);
  await seedInitial(term);
  const before = term.buffer.active.length;
  const frame = visibleRows(term);
  await writeAsync(term, buildRedraw(frame));
  const after = term.buffer.active.length;
  return { name, before, after, grew: after - before, dups: dupCount(dump(term)) };
}

const shapes = [
  ["A CUP-absolute   ", redrawCUP],
  ["B SU-scroll       ", redrawSU],
  ["C LF-scroll       ", redrawLF],
  ["D tmux-attach(real)", redrawTmuxAttach],
];

console.log(`seed=${INITIAL_LINES} @ ${COLS}x${ROWS}; seamless reconnect (no reset)\n`);
console.log("shape                 patchON grew/dups     patchOFF grew/dups   verdict");
for (const [name, fn] of shapes) {
  const on = await run(name, fn, true);
  const off = await run(name, fn, false);
  let verdict;
  if (on.dups === 0 && off.dups === 0) verdict = "no dup (both)";
  else if (on.dups > 0 && off.dups === 0) verdict = "PATCH is the amplifier";
  else if (on.dups > 0 && off.dups > 0) verdict = "dups NATIVE — patch NOT the cause";
  else verdict = "??";
  console.log(
    `${name}   +${String(on.grew).padStart(3)}/${String(on.dups).padStart(3)}` +
    `            +${String(off.grew).padStart(3)}/${String(off.dups).padStart(3)}` +
    `         ${verdict}`,
  );
}
