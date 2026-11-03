// The replay seam with tmux's status line (2026-11-03).
//
// On a full replay the backend sends a capture-pane blob, then tmux's attach
// output erases the client's screen (ESC[H ESC[2J — ED2 passes the client's
// filters; only ED3 and the alt-screen switch are dropped) and repaints the
// PANE. The client's terminal is the pane plus tmux's status line, so tmux
// repaints one row fewer than the client shows. With the blob ending exactly
// at its last row (the dup-seam.mjs rule, which assumed no status line), the
// client's top row, the last history line, is erased and never repainted: one
// line lost per full replay. Ending the blob with one CRLF per status row
// keeps it in scrollback, with no duplicate.
//
//   node test/seam-status-row.mjs      (exits 1 on failure)

import pkg from "@xterm/headless";
const { Terminal } = pkg;

const COLS = 40, ROWS = 20, STATUS = 1, PANE = ROWS - STATUS, HIST = 30;
const all = Array.from({ length: HIST + PANE }, (_, i) => `L${String(i + 1).padStart(2, "0")}`);
const pane = all.slice(-PANE);

function makeTerm() {
  const t = new Terminal({ cols: COLS, rows: ROWS, scrollback: 10000, allowProposedApi: true });
  const ALT = new Set([47, 1047, 1048, 1049]);
  const first = (ps) => (Array.isArray(ps[0]) ? ps[0][0] : ps[0]);
  t.parser.registerCsiHandler({ prefix: "?", final: "h" }, (ps) => ALT.has(first(ps)));
  t.parser.registerCsiHandler({ prefix: "?", final: "l" }, (ps) => ALT.has(first(ps)));
  t.parser.registerCsiHandler({ final: "J" }, (ps) => first(ps) === 3);
  return t;
}
const write = (t, d) => new Promise((r) => t.write(d, r));
const contents = (t) => {
  const b = t.buffer.active, out = [];
  for (let i = 0; i < b.length; i++) out.push(b.getLine(i)?.translateToString(true).trim() ?? "");
  return out.filter((l) => /^L\d\d$/.test(l));
};

// tmux attach: alt-screen (dropped by the client), erase, repaint the pane
// rows from the top, then the status line on the last row.
function attachOutput() {
  let s = "\x1b[?1049h\x1b[H\x1b[2J";
  pane.forEach((l, r) => { s += `\x1b[${r + 1};1H${l}\x1b[K`; });
  return s + `\x1b[${ROWS};1H[status]\x1b[K`;
}

async function run(trailing) {
  const t = makeTerm();
  await write(t, all.join("\r\n") + "\r\n".repeat(trailing));
  await write(t, attachOutput());
  const got = contents(t);
  return { missing: all.filter((l) => !got.includes(l)), dups: got.filter((l, i) => got.indexOf(l) !== i) };
}

const before = await run(0);
const after = await run(STATUS);
console.log("no trailing CRLF:   ", before);
console.log(`${STATUS} CRLF per status row:`, after);
const ok = before.missing.length === 1 && after.missing.length === 0 && after.dups.length === 0;
console.log(ok ? "OK: the status-row CRLF keeps the last history line" : "FAIL");
process.exit(ok ? 0 : 1);
