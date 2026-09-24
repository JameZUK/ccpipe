// PWA share_target: when the user shares text/URL/title from another
// app into ccpipe, the launch URL is /?text=…&url=…&title=…. We snag
// those values once into sessionStorage so the composer's onmount path
// can offer them — but we now ASK before pasting rather than dropping
// arbitrary text straight into the prompt (the composer feeds a shell).
//
// Two deliberate changes from the pre-fix version:
//   1. We DO NOT call history.replaceState() any more. The URL keeps
//      its query params so the operator can see exactly where the text
//      came from before they accept it. Silently scrubbing the URL was
//      what made the previous behaviour a usable social-engineering
//      vector (e.g. a Slack link preview that would silently pre-fill
//      a destructive command).
//   2. The consumer must call ``commitPendingShare()`` to actually take
//      the text — peek + commit are separate, so a caller can render
//      a review prompt first.
//
// Lives in its own module (rather than main.ts) so mobile.ts can use it
// without a main ↔ mobile import cycle. main.ts calls
// capturePendingShare() at module load, as before.

const KEY = "ccpipe.pendingShare";

export function capturePendingShare(): void {
  try {
    const params = new URLSearchParams(location.search);
    const parts: string[] = [];
    for (const key of ["title", "text", "url"]) {
      const v = params.get(key);
      if (v) parts.push(v);
    }
    if (parts.length === 0) return;
    sessionStorage.setItem(KEY, parts.join("\n"));
    // Intentionally NOT calling history.replaceState() — see comment.
  } catch {}
}

/** Peek at pending shared text without consuming it. */
export function peekPendingShare(): string | null {
  try {
    return sessionStorage.getItem(KEY);
  } catch { return null; }
}

/** Drop any pending shared text without inserting it (user dismissed). */
export function discardPendingShare(): void {
  try { sessionStorage.removeItem(KEY); } catch {}
}

/** Read the pending shared text and clear it. Use only after the user
 * has explicitly opted in to inserting it into the composer. */
export function commitPendingShare(): string | null {
  try {
    const v = sessionStorage.getItem(KEY);
    if (v) sessionStorage.removeItem(KEY);
    return v;
  } catch { return null; }
}
