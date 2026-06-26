// Adversarial verification of CANDIDATE [full-reconnect-visible-overlap].
//
// Models the FULL reconnect path (gap > SEAMLESS_MAX_GAP_MS):
//   1. resetTerminal()  -> fresh buffer, empty scrollback (main.ts onHello)
//   2. write the capture-pane blob (backend _capture_session_history:
//      `-S -10000` with NO `-E`, so the VISIBLE pane IS included; trailing
//      CRLF already stripped per the deployed fix)
//   3. write tmux's attach redraw, using the REAL shape captured from
//      tmux 3.6b in the prior phase:
//        per pass: (?1049h suppressed) CSI H + CSI J(ED0, in-place clear)
//                  + CSI 1;ROWS r (scroll region) + CSI H
//                  + for EVERY row: <text> + CSI K(EL) + CRLF
//      tmux was observed to do this redraw in 2 passes on attach.
//
// The candidate claims the visible region is "presented twice" (capture +
// redraw) and that the redraw duplicates the visible block into scrollback.
// We measure dupCount deterministically for 1 and 2 redraw passes, and with
// vs without a trailing CRLF after the final row.
//
// Usage: node test/dup-fullreconnect.mjs

import pkg from "@xterm/headless";
const { Terminal } = pkg;

const ROWS = 44, COLS = 60;
const INITIAL_LINES = 80;   // last ROWS visible, rest in scrollback

function writeAsync(term, data) {
  return new Promise((r) => term.write(data, r));
}

// Production-faithful Terminal: alt-screen + ED3 suppression + SU patch.
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
  if (!suPatchInstalled) throw new Error("SU patch failed to install");
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

function dupDetail(lines) {
  const seen = new Map();
  for (const l of lines) if (l) seen.set(l, (seen.get(l) || 0) + 1);
  const out = [];
  for (const [l, n] of seen) if (n > 1) out.push(`${JSON.stringify(l)} x${n}`);
  return out;
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

function lineText(i) { return `LINE ${String(i).padStart(3, "0")} content`; }

// The capture-pane blob: all INITIAL_LINES, CRLF-joined, NO trailing CRLF
// (matches _capture_session_history after the deployed trailing-CRLF strip).
function captureBlob() {
  let s = "";
  for (let i = 0; i < INITIAL_LINES; i++) {
    s += lineText(i);
    if (i < INITIAL_LINES - 1) s += "\r\n";
  }
  return s;
}

// One tmux attach redraw pass over `rows` (the CURRENT visible pane), using
// the REAL captured shape: in-place ED0 clear, scroll region, repaint each
// row from home with EL + CRLF. `trailingCRLF` controls whether the final
// row is followed by a CRLF (tmux was observed to emit CRLF after every row).
function redrawPass(rows, trailingCRLF) {
  let s = "\x1b[?1049h";          // alt-screen enter (suppressed by frontend)
  s += "\x1b[H";                  // home
  s += "\x1b[J";                  // ED0: erase cursor->end, IN PLACE (no scroll)
  s += `\x1b[1;${ROWS}r`;         // DECSTBM scroll region, top=0
  s += "\x1b[H";                  // home
  for (let r = 0; r < rows.length; r++) {
    s += rows[r] + "\x1b[K";
    if (r < rows.length - 1 || trailingCRLF) s += "\r\n";
  }
  s += "\x1b[r";                  // reset scroll region
  return s;
}

async function run(label, passes, trailingCRLF) {
  const term = makeTerm();
  // 1. reset already implicit (fresh term). 2. capture blob.
  await writeAsync(term, captureBlob());
  const before = term.buffer.active.length;
  const dupsBefore = dupCount(dump(term));
  // 3. tmux attach redraw. REAL tmux repaints its SERVER-SIDE pane
  //    snapshot, which does NOT change between passes (the client-side
  //    scroll from a trailing LF is invisible to tmux). So capture the
  //    frame ONCE and repaint that SAME fixed snapshot on every pass.
  const frame = visibleRows(term);   // == tail of the capture blob
  for (let p = 0; p < passes; p++) {
    await writeAsync(term, redrawPass(frame, trailingCRLF));
  }
  const after = term.buffer.active.length;
  const lines = dump(term);
  return {
    label, before, after, grew: after - before,
    dupsBefore, dupsAfter: dupCount(lines), detail: dupDetail(lines),
  };
}

console.log(`FULL reconnect: reset + capture(${INITIAL_LINES} lines) + tmux attach redraw @ ${COLS}x${ROWS}\n`);
const cases = [
  ["1 pass,  no trailing CRLF", 1, false],
  ["1 pass,  trailing CRLF   ", 1, true],
  ["2 passes,no trailing CRLF", 2, false],
  ["2 passes,trailing CRLF   ", 2, true],
];
for (const [label, passes, tc] of cases) {
  const r = await run(label, passes, tc);
  console.log(
    `${r.label}  bufLen ${String(r.before).padStart(3)}->${String(r.after).padStart(3)}  grew ${String(r.grew).padStart(3)}  dupsAfter ${String(r.dupsAfter).padStart(3)}` +
    (r.detail.length ? `   DUP: ${r.detail.join(", ")}` : ""),
  );
}
