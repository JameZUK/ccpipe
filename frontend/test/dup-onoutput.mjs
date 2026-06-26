// Adversarial verification of CANDIDATE [onoutput-no-dedup].
//
// Models the REAL ccpipe pipeline ordering proven from backend/ccpipe/ws.py:
//   1. hello                       (ws.py:332)
//   2. history blob                (ws.py:491)  <- dropped on seamless (ws.ts:340)
//   3. stream_ready                (ws.py:505)  <- frontend sets receivingHistory=false
//   4. PtyProcess (tmux attach)    (ws.py:543)  <- spawned AFTER stream_ready
//        => tmux's attach REDRAW arrives as LIVE output, post-stream_ready,
//           so it is NOT covered by the seamless history-drop. It reaches
//           onOutput unconditionally and is written to the preserved buffer.
//
// Redraw SHAPE matches the captured tmux 3.6b on-attach bytes (prior phase):
//   ZERO CSI S. The scroll primitive is the trailing LF at the bottom row of
//   an active DECSTBM scroll region (CSI 1;<rows>r ; home ; per-row text+EL+CRLF).
//   That LF-at-scroll-region-bottom routes through xterm's native scroll =>
//   bufferService.scroll() => pushes the (already-present) top visible line
//   into scrollback. One dup per scrolled row = one screen per reconnect.
//
// We reproduce the dup, then prove a candidate FIX (suppress scrollback
// GROWTH during the seamless attach-redraw window by wrapping
// _bufferService.scroll) drops dupsAfter to 0 while leaving the visible
// frame byte-identical.
//
// Usage: node test/dup-onoutput.mjs

import pkg from "@xterm/headless";
const { Terminal } = pkg;

const ROWS = 44, COLS = 60;
const INITIAL_LINES = 80;

function writeAsync(term, data) {
  return new Promise((r) => term.write(data, r));
}

// Build a Terminal wired like production terminal.ts, with an OPTIONAL
// "absorb seamless redraw" guard wrapping _bufferService.scroll (the
// funnel for BOTH the SU patch and native-LF scroll).
function makeTerm({ withFix = false } = {}) {
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
  term.parser.registerCsiHandler({ final: "J" }, (p) => firstParam(p) === 3);
  term.parser.registerCsiHandler({ prefix: "?", final: "J" }, (p) => firstParam(p) === 3);

  const core = term._core;
  const ih = core?._inputHandler;
  const bs = core?._bufferService;

  // ── production CSI SU (scrollUp) patch ──
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
    throw new Error("SU patch failed to install");
  }

  // ── CANDIDATE FIX: wrap _bufferService.scroll so that while the
  //    "absorb seamless redraw" window is armed, the scroll shifts the
  //    visible region IN PLACE without growing the scrollback ring.
  //    This is the single funnel for both the SU-patch path above and
  //    xterm's native LF/IND scroll (the primitive tmux actually uses).
  let absorbRedraw = false;
  if (withFix) {
    const realScroll = bs.scroll.bind(bs);
    bs.scroll = function (eraseAttr, isWrapped) {
      if (!absorbRedraw) return realScroll(eraseAttr, isWrapped);
      // During the seamless attach-redraw window the redraw repaints the
      // already-present frame; the trailing LF that lands on the bottom
      // scroll-region row is a cursor artifact, not genuine new content.
      // Suppress the scroll entirely so the freshly-repainted frame stays
      // in place — no scrollback growth, no content loss.
      return;
    };
  }
  return {
    term,
    armAbsorb: () => { absorbRedraw = true; },
    disarmAbsorb: () => { absorbRedraw = false; },
  };
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

// tmux on-attach redraw, shape from the captured wf_dup_8421 bytes:
// set DECSTBM region top=0, home, then per visible row: text + EL + CRLF.
// The CRLF that lands on the bottom scroll-region row scrolls the region.
function tmuxAttachRedraw(rows, passes = 1) {
  let s = "";
  for (let pass = 0; pass < passes; pass++) {
    s += "\x1b[?1049h";            // alt-screen enter (suppressed by frontend)
    s += "\x1b[H\x1b[J";          // home + ED0 (erase to end, in place)
    s += `\x1b[1;${ROWS}r`;      // DECSTBM region, top=1 => scrollTop=0
    s += "\x1b[H";                // home
    for (let r = 0; r < rows.length; r++) {
      s += rows[r] + "\x1b[K";    // row text + EL
      s += "\r\n";                // CRLF — advances; at bottom row this scrolls
    }
    s += "\x1b[r";                // reset scroll region
    s += "\x1b[?1049l";          // alt-screen leave (suppressed)
  }
  return s;
}

// Simulate a seamless reconnect end-to-end with the production ws.ts
// dispatch logic for the post-stream_ready redraw.
async function run({ withFix, passes = 1, reconnects = 1 }) {
  const { term, armAbsorb, disarmAbsorb } = makeTerm({ withFix });
  await seedInitial(term);

  const beforeLen = term.buffer.active.length;
  const dupsBefore = dupCount(dump(term));
  const frameBefore = visibleRows(term);     // what the redraw re-emits

  // SEAMLESS reconnect: hello (no reset) -> history blob DROPPED ->
  // stream_ready (receivingHistory=false) -> tmux attach redraw arrives
  // as LIVE output and is forwarded to onOutput unconditionally.
  for (let rc = 0; rc < reconnects; rc++) {
    const frame = visibleRows(term);   // re-read: redraw re-emits CURRENT frame
    const blob = tmuxAttachRedraw(frame, passes);
    if (withFix) armAbsorb();
    await writeAsync(term, blob);
    if (withFix) disarmAbsorb();
  }
  const afterLen = term.buffer.active.length;
  const dupsAfter = dupCount(dump(term));
  const frameAfter = visibleRows(term);
  const frameIntact = JSON.stringify(frameBefore) === JSON.stringify(frameAfter);

  return { beforeLen, afterLen, grew: afterLen - beforeLen,
           dupsBefore, dupsAfter, frameIntact };
}

console.log(`seed=${INITIAL_LINES} @ ${COLS}x${ROWS}; seamless reconnect; `
  + `redraw = tmux-attach LF-at-scroll-region-bottom (real captured shape)\n`);

const fmt = (r) =>
  `bufLen ${r.beforeLen}->${r.afterLen} (grew ${r.grew})  `
  + `dups ${r.dupsBefore}->${r.dupsAfter}  visibleFrameIntact=${r.frameIntact}`;

for (const passes of [1, 2, 3]) {
  const broken = await run({ withFix: false, passes, reconnects: 1 });
  console.log(`passes=${passes}, 1 reconnect, NO FIX:  ${fmt(broken)}`);
}
console.log("");
// 20 seamless reconnects, 3 passes each — accumulation over a mobile storm.
const storm = await run({ withFix: false, passes: 3, reconnects: 20 });
console.log(`passes=3, 20 reconnects, NO FIX:  ${fmt(storm)}`);
const stormFix = await run({ withFix: true, passes: 3, reconnects: 20 });
console.log(`passes=3, 20 reconnects, FIX:     ${fmt(stormFix)}`);
