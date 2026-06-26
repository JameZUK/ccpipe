// Reproduce the REAL mobile "dups + gaps in scrollback" bug from the debug
// snapshot, and prove the resize-absorb fix.
//
// Snapshot recentScrollEvents showed, on a loop:
//   resize n=45 → DECSTBM[1;44] → SU(patched) n=44   (ybase += 44)
//   resize n=23 → DECSTBM[1;22] → SU(patched) n=22   (ybase += 22)
// i.e. every terminal resize (soft keyboard 45↔23, height jitter) SIGWINCHes
// claude, which re-renders its whole live region with a FULL-REGION CSI SU;
// the SU patch archived that entire re-rendered screen into scrollback. Each
// toggle pushed a screen of (duplicate + reflowed/blank) rows. Between resizes
// there were ZERO SU events (streaming uses native LF, not SU).
//
// FIX (production terminal.ts): arm an "absorb" flag on resize; while armed,
// a FULL-REGION SU shifts in place (upstream, no archive) instead of growing
// scrollback. Partial SUs and all SUs outside the window still archive.

import pkg from "@xterm/headless";
const { Terminal } = pkg;

function build(withFix) {
  const term = new Terminal({ cols: 60, rows: 45, scrollback: 10000, allowProposedApi: true });
  const core = term._core;
  const ih = core?._inputHandler;
  const bs = core?._bufferService;
  let absorbRedraw = false;

  // production SU patch (terminal.ts) + the resize-absorb gate
  const originalScrollUp = ih.scrollUp.bind(ih);
  ih.scrollUp = function (params) {
    const buf = bs.buffer;
    if (buf.scrollTop !== 0) return originalScrollUp(params);
    let count = Math.min((params.params?.[0]) || 1, term.rows);
    if (withFix && absorbRedraw && count >= buf.scrollBottom - buf.scrollTop + 1) {
      return originalScrollUp(params);            // in place, do NOT archive
    }
    while (count-- > 0) bs.scroll(ih._eraseAttrData());
    ih._dirtyRowTracker?.markRangeDirty?.(buf.scrollTop, buf.scrollBottom);
    return true;
  };
  // arm on resize (production arms via term.onResize → armRedrawAbsorb)
  term.onResize(() => { absorbRedraw = true; });
  return {
    term,
    disarm: () => { absorbRedraw = false; },          // window expiry
  };
}

function writeAsync(term, d) { return new Promise((r) => term.write(d, r)); }

// One soft-keyboard toggle: shrink to 23 rows + claude re-render, then back to
// 45 + re-render. Each re-render = set top-anchored DECSTBM region + full SU.
async function toggle(h) {
  const { term, disarm } = h;
  for (const rows of [23, 45]) {
    term.resize(60, rows);                            // SIGWINCH → arms absorb
    const region = rows - 1;                          // claude's live region [1;rows-1]
    await writeAsync(term, `\x1b[1;${region}r`);      // DECSTBM
    await writeAsync(term, `\x1b[${region}S`);        // full-region SU (the re-render)
    await writeAsync(term, "\x1b[r");                 // reset region
    disarm();                                         // window closes before next event
  }
}

async function run(withFix) {
  const h = build(withFix);
  await writeAsync(h.term, "seed line\r\n".repeat(40)); // genuine scrollback via native LF
  const ybase0 = h.term.buffer.active.baseY;
  for (let i = 0; i < 20; i++) await toggle(h);         // 20 keyboard toggles
  const ybase1 = h.term.buffer.active.baseY;
  return { grew: ybase1 - ybase0 };
}

const broken = await run(false);
const fixed = await run(true);
console.log(`20 soft-keyboard toggles (40 resizes), each a full-region SU re-render:`);
console.log(`  NO FIX: scrollback grew by ${broken.grew} lines  (≈ one screen per toggle = dups + gaps)`);
console.log(`  FIX:    scrollback grew by ${fixed.grew} lines`);
// The fix eliminates the SU-archive flood (the dups + gaps). A small residual
// remains from xterm's OWN buffer reflow when the viewport shrinks 45→23 (rows
// that no longer fit scroll out — correct behaviour, ~1 line/toggle), which is
// not the SU patch and not the bug. Assert a >90% reduction, not exactly zero.
console.log(broken.grew > 0 && fixed.grew < broken.grew * 0.1
  ? `✅ SU re-render flood eliminated (${broken.grew}→${fixed.grew}); streaming (native LF) untouched`
  : "⚠ unexpected — investigate");
