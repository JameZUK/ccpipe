// Deterministic xterm scrollback replay harness.
//
// Why this exists: the "scrollback is scrambled / massive spaces and
// gaps on mobile" class of bug is deterministic in the BYTE STREAM a
// session replays on attach, but it was historically only reproducible
// by re-checking on a physical phone — so every fix was blind. This
// harness replays a captured byte stream through a HEADLESS xterm.js
// (same 5.5.0 parser as production) at an arbitrary cols×rows and
// measures the resulting scrollback for the gap symptom, so the bug
// can be reproduced and a fix verified offline in CI.
//
// A fixture is the raw output of `tmux capture-pane -p -e -J -S -N`,
// i.e. exactly what backend `_capture_session_history` captures before
// it LF→CRLF normalises and ships it to the browser. We apply the same
// normalisation here so the bytes match what xterm actually receives.
//
// Usage:
//   node test/replay-harness.mjs <fixture> --cols 50 --rows 40 [--json] [--dump]

import { readFileSync } from "node:fs";
import pkg from "@xterm/headless";
const { Terminal } = pkg;

function parseArgs(argv) {
  const a = { cols: 50, rows: 40, json: false, dump: false, fixture: null };
  for (let i = 0; i < argv.length; i++) {
    const t = argv[i];
    if (t === "--cols") a.cols = parseInt(argv[++i], 10);
    else if (t === "--rows") a.rows = parseInt(argv[++i], 10);
    else if (t === "--json") a.json = true;
    else if (t === "--dump") a.dump = true;
    else if (!t.startsWith("--")) a.fixture = t;
  }
  if (!a.fixture) {
    console.error("usage: node replay-harness.mjs <fixture> --cols N --rows N [--json] [--dump]");
    process.exit(2);
  }
  return a;
}

// Mirror backend ws.py::_capture_session_history normalisation: tmux
// joins lines with LF; xterm wants CRLF so each line starts at column 0.
// Operate at the BYTE level and hand the bytes to xterm as-is — the real
// frontend writes a Uint8Array and xterm decodes UTF-8 itself (box-drawing
// and emoji glyphs are multibyte and must NOT be split into code points).
function normaliseToCRLF(buf) {
  // bytes: replace \r\n -> \n, then \n -> \r\n. Done via string over
  // latin1 (1 char == 1 byte) purely for the newline rewrite, then back
  // to bytes — newlines are ASCII so this never touches multibyte runs.
  let s = buf.toString("latin1").replace(/\r\n/g, "\n").replace(/\n/g, "\r\n");
  if (!s.endsWith("\r\n")) s += "\r\n";
  return Buffer.from(s, "latin1");       // bytes preserved; UTF-8 intact
}

// Build a Terminal configured like production createTerminal(): same
// scrollback, alt-screen suppression, and ED3-wipe suppression. The SU
// scrollback patch is irrelevant to a plain history-replay (no CSI SU
// in a capture-pane blob) so it's omitted; this harness targets the
// history-replay path specifically.
function makeTerm(cols, rows) {
  const term = new Terminal({
    cols,
    rows,
    scrollback: 10000,
    allowProposedApi: true,
    convertEol: false,
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
  return term;
}

function writeAsync(term, data) {
  return new Promise((resolve) => term.write(data, resolve));
}

// Pull the full active buffer (scrollback + visible) as rendered text.
function dumpBuffer(term) {
  const buf = term.buffer.active;
  const out = [];
  for (let i = 0; i < buf.length; i++) {
    const line = buf.getLine(i);
    out.push(line ? line.translateToString(true) : "");
  }
  return out;
}

// Gap metrics. A "blank" row is one that is empty after trailing
// whitespace is stripped (what the eye reads as a gap). We report the
// blank ratio and the longest consecutive run — the run length is the
// best single proxy for "massive spaces and gaps": a healthy document
// has the odd 1–2 line break; the bug produces long stretches.
function analyse(lines) {
  let blank = 0;
  let run = 0;
  let maxRun = 0;
  const runs = [];
  for (const raw of lines) {
    const stripped = raw.replace(/\s+$/u, "");
    if (stripped === "") {
      blank++;
      run++;
      if (run > maxRun) maxRun = run;
    } else {
      if (run >= 3) runs.push(run);
      run = 0;
    }
  }
  if (run >= 3) runs.push(run);
  return {
    totalLines: lines.length,
    blankLines: blank,
    blankRatio: lines.length ? +(blank / lines.length).toFixed(3) : 0,
    maxConsecutiveBlank: maxRun,
    gapRuns: runs.sort((a, b) => b - a),
  };
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const raw = readFileSync(args.fixture);
  const data = normaliseToCRLF(raw);
  const term = makeTerm(args.cols, args.rows);
  // Write as a Uint8Array — xterm decodes UTF-8 internally, same as the
  // real frontend's writeToTerm(Uint8Array).
  await writeAsync(term, new Uint8Array(data));
  const lines = dumpBuffer(term);
  const metrics = analyse(lines);
  const result = { fixture: args.fixture, cols: args.cols, rows: args.rows, ...metrics };

  if (args.json) {
    console.log(JSON.stringify(result));
  } else {
    console.log(`fixture=${args.fixture} @ ${args.cols}x${args.rows}`);
    console.log(`  buffer lines     : ${metrics.totalLines}`);
    console.log(`  blank lines      : ${metrics.blankLines} (${(metrics.blankRatio * 100).toFixed(1)}%)`);
    console.log(`  max blank run    : ${metrics.maxConsecutiveBlank}`);
    console.log(`  gap runs (>=3)   : [${metrics.gapRuns.join(", ")}]`);
  }
  if (args.dump) {
    console.log("\n----- buffer dump (· = blank row) -----");
    for (const l of lines) {
      const s = l.replace(/\s+$/u, "");
      console.log(s === "" ? "·" : s);
    }
  }
}

main();
