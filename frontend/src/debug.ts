// Debug-snapshot affordance for the live terminal.
//
// Captures a single JSON blob describing the front-end's view of the
// world — WS counters, xterm buffer state, and (optionally) the last
// N lines of scrollback as plain text — and offers it for copy /
// download / POST-to-server. The server-side companion endpoint
// (/api/debug/snapshot) writes the report into the journal next to
// the live WsCounters for the same session so we can compare the
// two sides at the exact same moment.
//
// Trigger surfaces:
//   - Ctrl+Shift+D keyboard shortcut (set up in main.ts)
//   - "Capture diagnostic snapshot" button in Settings → Debug
//
// We do NOT auto-redact — the buffer dump can include whatever was
// last on the user's screen. The modal warns about that before the
// user copies / shares.

import { apiJson } from "./api";
import type { TerminalSocket } from "./ws";

export interface DebugCapableTerminal {
  getDebugState(): Record<string, unknown>;
  dumpBuffer(maxLines?: number): string[];
}

export interface DebugCaptureOpts {
  session: string;
  terminal: DebugCapableTerminal;
  socket: TerminalSocket;
  note?: string;
  /** How many lines of scrollback to include. Default 500 — enough to
   * see a few screens of context without making the JSON unwieldy. */
  maxBufferLines?: number;
}

export interface DebugSnapshot {
  schema: 1;
  capturedAt: string;          // ISO timestamp
  performanceNow: number;      // monotonic, for cross-frame correlation
  session: string;
  note: string;
  userAgent: string;
  url: string;
  viewport: { innerWidth: number; innerHeight: number; devicePixelRatio: number };
  terminal: Record<string, unknown>;
  socket: Record<string, unknown>;
  buffer: { lineCount: number; tail: string[] };
}

export function captureSnapshot(opts: DebugCaptureOpts): DebugSnapshot {
  const maxLines = opts.maxBufferLines ?? 500;
  const tail = opts.terminal.dumpBuffer(maxLines);
  return {
    schema: 1,
    capturedAt: new Date().toISOString(),
    performanceNow: performance.now(),
    session: opts.session,
    note: opts.note ?? "",
    userAgent: navigator.userAgent,
    url: location.href,
    viewport: {
      innerWidth: window.innerWidth,
      innerHeight: window.innerHeight,
      devicePixelRatio: window.devicePixelRatio,
    },
    terminal: opts.terminal.getDebugState(),
    socket: opts.socket.getDebugState(),
    buffer: { lineCount: tail.length, tail },
  };
}

/** POST the snapshot to /api/debug/snapshot. Returns the server's
 * acknowledgement (which includes its view of the same session's
 * WsCounters) so the caller can render the merged record. */
export async function postSnapshot(
  snapshot: DebugSnapshot,
): Promise<{ backend: Record<string, unknown>; active_sessions_for_name: number }> {
  return apiJson("/api/debug/snapshot", {
    method: "POST",
    body: JSON.stringify({
      session: snapshot.session,
      note: snapshot.note,
      payload: snapshot,
    }),
  });
}

/** Show a modal with the snapshot JSON, action buttons (copy /
 * download / post-to-server / close), and the merged backend view
 * once the user posts. Returns when the modal is dismissed. */
export function showDebugModal(snapshot: DebugSnapshot): void {
  // Pretty-print with a 2-space indent so the JSON is human-readable
  // in the modal. The tail array shows up one line per scrollback row,
  // which is exactly what we want for visual scanning.
  let json = JSON.stringify(snapshot, null, 2);

  const overlay = document.createElement("div");
  overlay.className = "modal-overlay debug-modal__overlay";
  overlay.innerHTML = `
    <div class="modal debug-modal" role="dialog" aria-modal="true" aria-label="Diagnostic snapshot">
      <div class="modal__header">
        <h2 class="modal__title">Diagnostic snapshot</h2>
        <button type="button" class="modal__close" data-role="close" aria-label="Close">×</button>
      </div>
      <div class="modal__body debug-modal__body">
        <p class="debug-modal__warning">
          Includes the last ${snapshot.buffer.lineCount} lines of your terminal as plain text.
          Review before pasting anywhere shared.
        </p>
        <label class="row">
          <span class="row__label">Note (optional)</span>
          <input type="text" class="text-input" data-role="note"
                 placeholder="What went wrong? e.g. 'missing scrollback after reconnect'"
                 value="${escapeAttr(snapshot.note)}"/>
        </label>
        <textarea class="debug-modal__json" readonly spellcheck="false">${escapeText(json)}</textarea>
        <div class="debug-modal__server" data-role="server-status"></div>
      </div>
      <div class="modal__row-actions">
        <button type="button" class="btn btn--ghost" data-role="copy">Copy JSON</button>
        <button type="button" class="btn btn--ghost" data-role="download">Download</button>
        <button type="button" class="btn" data-role="post">Send to server log</button>
      </div>
    </div>
  `;
  document.body.appendChild(overlay);

  const close = () => {
    overlay.remove();
    document.removeEventListener("keydown", onKey);
  };
  const onKey = (e: KeyboardEvent) => {
    if (e.key === "Escape") close();
  };
  document.addEventListener("keydown", onKey);
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) close();
  });
  overlay.querySelector<HTMLButtonElement>("[data-role=close]")!
    .addEventListener("click", close);

  const noteEl = overlay.querySelector<HTMLInputElement>("[data-role=note]")!;
  const jsonEl = overlay.querySelector<HTMLTextAreaElement>(".debug-modal__json")!;
  const serverEl = overlay.querySelector<HTMLElement>("[data-role=server-status]")!;
  // Live re-render of the JSON when the user types in the note field —
  // means they can edit the label and the resulting copy/download/post
  // all carry the latest text.
  const refresh = () => {
    snapshot.note = noteEl.value;
    json = JSON.stringify(snapshot, null, 2);
    jsonEl.value = json;
  };
  noteEl.addEventListener("input", refresh);

  overlay.querySelector<HTMLButtonElement>("[data-role=copy]")!
    .addEventListener("click", async () => {
      refresh();
      try {
        await navigator.clipboard.writeText(json);
        serverEl.textContent = "Copied to clipboard.";
      } catch (err) {
        serverEl.textContent = `Copy failed: ${(err as Error).message}`;
      }
    });

  overlay.querySelector<HTMLButtonElement>("[data-role=download]")!
    .addEventListener("click", () => {
      refresh();
      const blob = new Blob([json], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `ccpipe-snapshot-${snapshot.session || "anon"}-${snapshot.capturedAt.replace(/[:.]/g, "-")}.json`;
      a.click();
      URL.revokeObjectURL(url);
      serverEl.textContent = "Downloaded.";
    });

  overlay.querySelector<HTMLButtonElement>("[data-role=post]")!
    .addEventListener("click", async (e) => {
      refresh();
      const btn = e.target as HTMLButtonElement;
      btn.disabled = true;
      serverEl.textContent = "Sending…";
      try {
        const ack = await postSnapshot(snapshot);
        const ackJson = JSON.stringify(ack, null, 2);
        serverEl.textContent = `Server ack:\n${ackJson}`;
      } catch (err) {
        serverEl.textContent = `Send failed: ${(err as Error).message}`;
      } finally {
        btn.disabled = false;
      }
    });
}

function escapeText(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
function escapeAttr(s: string): string {
  return escapeText(s).replace(/"/g, "&quot;");
}
