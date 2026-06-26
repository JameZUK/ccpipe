// Adversarial verification of CANDIDATE [seamless-redraw-after-streamready].
//
// Claim under test: ws.ts's seamless content-drop
//   `if (this.seamlessReconnect) return;`  (ws.ts:340)
// is nested INSIDE `if (this.receivingHistory)` (ws.ts:334), and
// receivingHistory is cleared on `stream_ready` (ws.ts:252). Because the
// backend spawns the `tmux attach-session` relay ONLY AFTER it has sent
// stream_ready (ws.py:505 then :543), tmux's attach redraw arrives as
// LIVE PTY frames when receivingHistory is already false. So the seamless
// drop never filters the one frame stream most likely to repaint
// already-present content -> it is written over the preserved buffer and
// duplicates scrollback.
//
// This harness reproduces the EXACT ws.ts onmessage state machine (the
// receivingHistory + seamlessReconnect gate) and feeds it the real server
// message ordering: hello -> history-blob frame -> stream_ready ->
// attach-redraw frame. It uses the SAME xterm wiring as terminal.ts
// (alt-screen + ED3 suppression + scrollUp patch).
//
// Then it tests the proposed FIX (extend the seamless drop to also cover
// post-stream_ready frames until the live keystroke stream begins) and
// shows dup count -> 0.
//
// Usage: node test/dup-streamready.mjs

import pkg from "@xterm/headless";
const { Terminal } = pkg;

const ROWS = 44, COLS = 60;
const INITIAL_LINES = 80;

const writeAsync = (term, data) => new Promise((r) => term.write(data, r));

function makeTerm() {
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
  const originalScrollUp = ih.scrollUp.bind(ih);
  ih.scrollUp = function (params) {
    const buf = bs.buffer;
    if (buf.scrollTop !== 0) return originalScrollUp(params);
    let count = Math.min((params.params?.[0]) || 1, term.rows);
    while (count-- > 0) bs.scroll(ih._eraseAttrData());
    ih._dirtyRowTracker?.markRangeDirty?.(buf.scrollTop, buf.scrollBottom);
    return true;
  };
  return term;
}

const dump = (term) => {
  const buf = term.buffer.active, out = [];
  for (let i = 0; i < buf.length; i++) {
    const l = buf.getLine(i);
    out.push(l ? l.translateToString(true).replace(/\s+$/u, "") : "");
  }
  return out;
};
const dupCount = (lines) => {
  const seen = new Map();
  for (const l of lines) if (l) seen.set(l, (seen.get(l) || 0) + 1);
  let d = 0; for (const [, n] of seen) if (n > 1) d += n - 1; return d;
};
const visibleRows = (term) => {
  const buf = term.buffer.active, out = [];
  for (let r = 0; r < term.rows; r++) {
    const l = buf.getLine(buf.baseY + r);
    out.push(l ? l.translateToString(true).replace(/\s+$/u, "") : "");
  }
  return out;
};

async function seedInitial(term) {
  let s = "";
  for (let i = 0; i < INITIAL_LINES; i++) {
    s += `LINE ${String(i).padStart(3, "0")} content`;
    if (i < INITIAL_LINES - 1) s += "\r\n";
  }
  await writeAsync(term, s);
}

const redrawCUP = (rows) => {
  let s = "\x1b[H";
  for (let r = 0; r < rows.length; r++) s += `\x1b[${r + 1};1H` + rows[r] + "\x1b[K";
  return s;
};

// Faithful tmux-on-attach redraw shape, per the captured wf_dup_8421 bytes:
// home + ED0 (in place) + set scroll region + home + per-row: text + EL + CRLF.
// The trailing CRLF at the scroll-region bottom row is the LF-at-bottom
// scroll primitive that pushes the preceding visible line into scrollback.
function redrawTmuxAttach(rows) {
  let s = "\x1b[H";                 // home
  s += "\x1b[J";                    // ED0 (erase to end, in place; no scroll, no scrollback wipe)
  s += `\x1b[1;${ROWS}r`;           // DECSTBM scroll region top=1
  s += "\x1b[H";                    // home
  for (let r = 0; r < rows.length; r++) {
    s += rows[r] + "\x1b[K\r\n";    // text + EL + CRLF (LF advances; at bottom row it scrolls)
  }
  s += "\x1b[r";                    // reset scroll region
  return s;
}

// SU-scroll shape (DECSTBM top=0 + CSI S, then repaint) — hits scrollUp patch.
function redrawSU(rows) {
  let s = `\x1b[1;${ROWS}r`;
  s += `\x1b[${rows.length}S`;
  s += redrawCUP(rows);
  s += "\x1b[r";
  return s;
}
// LF-scroll shape (park cursor at bottom, one LF per row, then repaint).
function redrawLF(rows) {
  let s = `\x1b[${ROWS};1H`;
  s += "\n".repeat(rows.length);
  s += redrawCUP(rows);
  return s;
}

// ── Faithful model of ws.ts TerminalSocket onmessage gating ──
// `fixMode` toggles the proposed fix.
class Socket {
  constructor(term, fixMode) {
    this.term = term;
    this.fixMode = fixMode;
    this.helloCount = 0;
    this.receivingHistory = false;
    this.seamlessReconnect = false;
    // FIX: a window that stays open past stream_ready until the first
    // genuine post-attach event (a keystroke/output the user caused),
    // covering the attach-redraw frames.
    this.suppressLiveRedraw = false;
  }
  onHello() {
    this.helloCount += 1;
    // seamless decision (helloCount>1 && small gap) — modelled as true here
    this.seamlessReconnect = this.helloCount > 1;
    this.receivingHistory = true;
    if (this.fixMode) this.suppressLiveRedraw = this.seamlessReconnect;
  }
  onStreamReady() {
    this.receivingHistory = false;
    // NOTE current code does NOT touch the seamless gate here.
  }
  // Returns true if the payload was written to the terminal.
  async onBinaryPtyFrame(payload) {
    if (this.receivingHistory) {
      if (this.seamlessReconnect) return false;   // ws.ts:340 — history drop
    } else if (this.fixMode && this.suppressLiveRedraw) {
      // FIX: keep dropping live frames during the attach-redraw window on a
      // seamless reconnect. Closed by an explicit signal (here: the test
      // calls endRedrawWindow()).
      return false;
    }
    await writeAsync(this.term, payload);
    return true;
  }
  endRedrawWindow() { this.suppressLiveRedraw = false; }
}

async function scenario(fixMode, buildRedraw) {
  const term = makeTerm();
  await seedInitial(term);
  const dupsBefore = dupCount(dump(term));
  const frame = visibleRows(term);
  const before = term.buffer.active.length;

  const sock = new Socket(term, fixMode);
  // First attach already happened during seed; simulate a SEAMLESS reconnect:
  sock.helloCount = 1;            // pretend a prior attach occurred
  // Server message ordering on the reconnect:
  sock.onHello();                                  // hello (receivingHistory=true, seamless=true)
  await sock.onBinaryPtyFrame(strBytes("HIST"));   // history blob frame (dropped by seamless)
  sock.onStreamReady();                            // stream_ready (receivingHistory=false)
  // tmux attach-session relay spawns AFTER stream_ready -> attach redraw as LIVE frame:
  const wrote = await sock.onBinaryPtyFrame(strBytes(buildRedraw(frame)));

  const after = term.buffer.active.length;
  const dupsAfter = dupCount(dump(term));
  return { fixMode, before, after, grew: after - before, dupsBefore, dupsAfter, redrawWritten: wrote };
}

function strBytes(s) { return new TextEncoder().encode(s); }

const shapes = [
  ["tmux-attach (CRLF-in-region)", redrawTmuxAttach],
  ["SU-scroll                   ", redrawSU],
  ["LF-scroll                   ", redrawLF],
  ["CUP-absolute (idempotent)   ", redrawCUP],
];

console.log(`seed=${INITIAL_LINES} @ ${COLS}x${ROWS}; SEAMLESS reconnect; redraw arrives AFTER stream_ready\n`);
console.log("redraw shape                    mode     written  bufLen        grew  dupsAfter");
for (const [name, fn] of shapes) {
  for (const fixMode of [false, true]) {
    const r = await scenario(fixMode, fn);
    console.log(
      `${name}   ${fixMode ? "FIXED  " : "CURRENT"}   ${String(r.redrawWritten).padStart(5)}` +
      `   ${String(r.before).padStart(3)}->${String(r.after).padStart(3)}    ${String(r.grew).padStart(4)}      ${String(r.dupsAfter).padStart(5)}`,
    );
  }
}
