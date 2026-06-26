// Deterministic replication of the seamless-vs-full decision in ws.ts.
// Models the exact ordering: at `hello` gap = now - lastDataAt; live AND
// dropped-history FRAME_PTY_OUTPUT frames both set lastDataAt (current code,
// line 333 is BEFORE the history-drop). We toggle whether dropped-history
// frames update lastDataAt to see if it changes the classification.
const SEAMLESS_MAX_GAP_MS = 15_000;

function run({ updateOnDroppedHistory }) {
  let lastDataAt = 0;
  let helloCount = 0;
  const decisions = [];

  // Simulate an event timeline. Each reconnect = {hello at t} followed by a
  // history blob (one frame at t+5ms) that is DROPPED (seamless) or written.
  // genuineOutput[] = times of real live PTY frames.
  function hello(t) {
    helloCount += 1;
    const gap = lastDataAt > 0 ? t - lastDataAt : Infinity;
    const seamless = helloCount > 1 && gap < SEAMLESS_MAX_GAP_MS;
    decisions.push({ t, gap, seamless });
    return seamless;
  }
  function historyFrame(t, seamless) {
    // current code sets lastDataAt regardless (line 333 before drop guard)
    if (!seamless) { lastDataAt = t; return; }       // full reconnect: written
    if (updateOnDroppedHistory) lastDataAt = t;        // BUG path
  }
  function liveFrame(t) { lastDataAt = t; }

  // Timeline: one genuine burst of live output at t=0..1000, then the session
  // goes IDLE. Reconnects fire every 10s (within SEAMLESS_MAX_GAP_MS of each
  // other) with NO further live output — the rapid-drop-while-idle case.
  liveFrame(0);
  liveFrame(1000);
  for (let i = 1; i <= 6; i++) {
    const t = 1000 + i * 10_000;           // 11s, 21s, 31s, ... apart by 10s
    const seamless = hello(t);
    historyFrame(t + 5, seamless);
  }
  return decisions;
}

for (const updateOnDroppedHistory of [true, false]) {
  const d = run({ updateOnDroppedHistory });
  const seamlessCount = d.filter((x) => x.seamless).length;
  console.log(`updateOnDroppedHistory=${updateOnDroppedHistory}  seamless=${seamlessCount}/${d.length}`);
  for (const x of d) console.log(`   t=${x.t} gap=${x.gap === Infinity ? "inf" : x.gap} -> ${x.seamless ? "SEAMLESS" : "full"}`);
}
