// The replay seam, history-only shape (2026-11-03, code review #12).
//
// The replay is the pane's HISTORY plus one blank line per client row;
// tmux's attach redraw then erases the screen and paints the pane. If
// lines scroll into history between the redraw (T0) and the capture
// (T1), the capture holds d rows that the held output after the redraw
// scrolls in again: d duplicates. ws._send_history drops them from the
// capture's end. This models both, with and without a status line.
//
//   node test/seam-history-only.mjs      (exits 1 on failure)

import pkg from "@xterm/headless";
const { Terminal } = pkg;

const COLS = 40, HIST = 60, D = 5;
const label = (i) => `L${String(i).padStart(3, "0")}`;

function makeTerm(rows) {
  const t = new Terminal({ cols: COLS, rows, scrollback: 10000, allowProposedApi: true });
  // The client's filters: alt-screen switches and ED3 dropped.
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
  return out.filter((l) => /^L\d{3}$/.test(l));
};

async function run({ rows, status, trim }) {
  const pane = rows - status;
  const all = Array.from({ length: HIST + pane + D }, (_, i) => label(i));
  // At T0 (the redraw): history all[0..HIST), screen all[HIST..HIST+pane).
  // Then D more lines are printed; by T1 (the capture) history has D more.
  const historyAtT1 = all.slice(0, HIST + D);
  const captured = trim ? historyAtT1.slice(0, historyAtT1.length - D) : historyAtT1;
  const t = makeTerm(rows);
  await write(t, captured.join("\r\n") + "\r\n".repeat(rows));
  // Held output: the T0 redraw (erase, pane rows, status line)...
  let redraw = "\x1b[?1049h\x1b[H\x1b[2J";
  all.slice(HIST, HIST + pane).forEach((l, r) => { redraw += `\x1b[${r + 1};1H${l}\x1b[K`; });
  if (status) redraw += `\x1b[${rows};1H[status]\x1b[K`;
  // ...then the D lines printed after it, scrolling the pane region.
  let more = `\x1b[1;${pane}r\x1b[${pane};1H`;
  for (const l of all.slice(HIST + pane)) more += `\r\n${l}`;
  more += "\x1b[r";
  await write(t, redraw + more);
  const got = contents(t);
  const missing = all.filter((l) => !got.includes(l));
  const dups = got.filter((l, i) => got.indexOf(l) !== i);
  const inOrder = got.every((l, i) => i === 0 || l > got[i - 1]);
  return { missing: missing.length, dups: dups.length, inOrder };
}

let ok = true;
for (const status of [1, 0]) {
  for (const rows of [24, 10]) {
    const untrimmed = await run({ rows, status, trim: false });
    const trimmed = await run({ rows, status, trim: true });
    const good = untrimmed.dups === D && untrimmed.missing === 0
      && trimmed.dups === 0 && trimmed.missing === 0 && trimmed.inOrder;
    ok &&= good;
    console.log(`${good ? "ok  " : "FAIL"} rows=${rows} status=${status}  untrimmed ${JSON.stringify(untrimmed)}  trimmed ${JSON.stringify(trimmed)}`);
  }
}
console.log(ok ? "OK: trimming the rows scrolled during the attach leaves each line once" : "FAIL");
process.exit(ok ? 0 : 1);
