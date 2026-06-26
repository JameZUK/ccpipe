// DECISIVE adversarial test for CANDIDATE [fresh-attach-redraw-every-reconnect].
//
// Replays the ACTUAL captured tmux-3.6b on-attach redraw bytes (recorded on
// isolated socket wf_dup_3307 via `script -q -c 'tmux attach'`) through a
// terminal wired EXACTLY like production frontend/src/terminal.ts
// (SU-scrollUp patch + alt-screen suppression + ED3 suppression), over a
// PRESERVED buffer (= SEAMLESS reconnect: no reset).
//
// The candidate claims the attach redraw scrolls existing visible content
// into scrollback (LF/SU caught by the patched scrollUp), duplicating a
// whole visible band per reconnect. The decisive signal is BUFFER GROWTH:
// if the redraw pushes rows into scrollback, buffer.length grows. We seed
// the buffer with UNIQUE marker lines (SEED-***) so the redraw's repaint
// (LINE-**) cannot coincidentally alias scrollback content.
//
// Pass the captured-bytes file as argv[2] (redraw-only or full stream).
// Usage: node test/dup-real-bytes-replay.mjs <bytes-file>

import fs from "node:fs";
import pkg from "@xterm/headless";
const { Terminal } = pkg;

const ROWS = 24, COLS = 80, SEED_LINES = 80; // 24 visible + 56 in scrollback

function writeAsync(term, data) { return new Promise((r) => term.write(data, r)); }

// Production-faithful wiring (mirrors terminal.ts).
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
  let suPatched = false;
  if (ih && bs && typeof ih.scrollUp === "function" && typeof bs.scroll === "function"
      && typeof ih._eraseAttrData === "function" && bs.buffer) {
    suPatched = true;
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
  return { term, suPatched };
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
async function seed(term) {
  let s = "";
  for (let i = 0; i < SEED_LINES; i++) {
    s += `SEED-${String(i).padStart(3, "0")}-marker-line`;
    if (i < SEED_LINES - 1) s += "\r\n";
  }
  await writeAsync(term, s);
}

const file = process.argv[2];
const bytes = new Uint8Array(fs.readFileSync(file));
const { term, suPatched } = makeTerm();
await seed(term);
const beforeLen = term.buffer.active.length;
const beforeDup = dupCount(dump(term));

// SEAMLESS reconnect: replay the real attach redraw with NO reset.
await writeAsync(term, bytes);

const afterLen = term.buffer.active.length;
const afterDup = dupCount(dump(term));

console.log(`file: ${file}  (${bytes.length} bytes)`);
console.log(`SU patch installed: ${suPatched}`);
console.log(`buffer.length  ${beforeLen} -> ${afterLen}   (grew ${afterLen - beforeLen})`);
console.log(`dupCount       ${beforeDup} -> ${afterDup}   (delta ${afterDup - beforeDup})`);
const verdict = (afterLen - beforeLen) > 0 || (afterDup - beforeDup) > 0
  ? `>> DUPLICATES: redraw pushed ${afterLen - beforeLen} rows into scrollback, +${afterDup - beforeDup} dup lines`
  : `   CLEAN: real attach redraw pushed NOTHING into scrollback (no growth, no new dups)`;
console.log(verdict);
