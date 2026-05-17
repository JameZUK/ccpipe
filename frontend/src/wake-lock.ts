// Screen wake-lock helper. Holding the lock prevents the phone display
// from dimming/sleeping during operations the user is actively waiting
// on — long claude responses being read aloud by TTS, or mic dictation
// where a sleep mid-utterance would drop the WS.
//
// Reference-counted: multiple callers can acquire, and the underlying
// lock is only released when every caller has called release(). The
// Page Visibility API releases the lock automatically when the tab is
// hidden (browser behaviour), so we re-acquire on visibility back.
//
// Feature-detected — iOS Safari < 16.4 has no wake-lock; acquire() is a
// no-op there.

type Sentinel = { released: boolean; release: () => Promise<void> };

let activeSentinel: Sentinel | null = null;
let refCount = 0;
let visibilityHooked = false;

const supported = typeof navigator !== "undefined"
  && "wakeLock" in navigator
  && typeof (navigator as unknown as { wakeLock: { request: unknown } }).wakeLock.request === "function";

async function _acquireUnderlying(): Promise<void> {
  if (!supported || activeSentinel) return;
  try {
    const s = await (navigator as unknown as {
      wakeLock: { request(type: "screen"): Promise<Sentinel> };
    }).wakeLock.request("screen");
    activeSentinel = s;
    // The browser auto-releases when the tab hides; re-acquire next time
    // it becomes visible if anyone still holds a ref.
    if (!visibilityHooked) {
      visibilityHooked = true;
      document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "visible" && refCount > 0 && !activeSentinel) {
          void _acquireUnderlying();
        }
      });
    }
  } catch (err) {
    // permissions or transient OS failure — log once, otherwise quiet.
    console.warn("wake-lock acquire failed:", err);
  }
}

async function _releaseUnderlying(): Promise<void> {
  if (!activeSentinel) return;
  try { await activeSentinel.release(); } catch {}
  activeSentinel = null;
}

/** Increment the ref-count; acquire the underlying lock if needed. */
export async function acquire(): Promise<void> {
  refCount += 1;
  if (refCount === 1) await _acquireUnderlying();
}

/** Decrement the ref-count; release the underlying lock at 0. */
export async function release(): Promise<void> {
  if (refCount <= 0) return;
  refCount -= 1;
  if (refCount === 0) await _releaseUnderlying();
}
