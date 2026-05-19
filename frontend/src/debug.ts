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
  /** Dump xterm's main + alternate buffers separately. The diff tool
   * compares like-to-like against tmux's two screens so alt-screen
   * apps (claude code's TUI) don't produce spurious "everything is
   * different" results. */
  dumpAllBuffers?: (maxLines?: number) => {
    activeType: string;
    normal: string[];
    alternate: string[];
  };
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
  schema: 2;
  capturedAt: string;          // ISO timestamp
  performanceNow: number;      // monotonic, for cross-frame correlation
  session: string;
  note: string;
  userAgent: string;
  url: string;
  viewport: { innerWidth: number; innerHeight: number; devicePixelRatio: number };
  terminal: Record<string, unknown>;
  socket: Record<string, unknown>;
  /** Backwards-compat: the active-buffer tail (same as schema 1).
   * New snapshots should prefer ``buffers`` below for diff purposes. */
  buffer: { lineCount: number; tail: string[] };
  /** schema-2: both xterm buffers + which is currently active.
   * Alt-screen apps (claude code's TUI, vim) switch between them
   * and a diff comparing across them is meaningless. */
  buffers?: {
    activeType: string;
    normal: string[];
    alternate: string[];
  };
}

export function captureSnapshot(opts: DebugCaptureOpts): DebugSnapshot {
  const maxLines = opts.maxBufferLines ?? 500;
  const tail = opts.terminal.dumpBuffer(maxLines);
  const buffers = opts.terminal.dumpAllBuffers
    ? opts.terminal.dumpAllBuffers(maxLines)
    : undefined;
  return {
    schema: 2,
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
    buffers,
  };
}

export interface DiffMismatchExample {
  line_from_bottom: number;
  frontend: string;
  backend: string;
}
export interface DiffResult {
  lines_compared: number;
  frontend_lines: number;
  backend_lines: number;
  matches: number;
  mismatches: number;
  mismatch_examples: DiffMismatchExample[];
}
export interface DebugSnapshotAck {
  backend: Record<string, unknown>;
  active_sessions_for_name: number;
  /** Content diff. Schema-2 has separate normal + alternate
   * sub-diffs; schema-1 has only `normal` with `alternate: null`. */
  content_diff: {
    active_type: string;
    normal: DiffResult | null;
    alternate: DiffResult | null;
  } | null;
}

/** POST the snapshot to /api/debug/snapshot. Returns the server's
 * acknowledgement: the same session's WsCounters PLUS a content
 * diff between the frontend buffer tail and what tmux currently
 * has for the pane. */
export async function postSnapshot(snapshot: DebugSnapshot): Promise<DebugSnapshotAck> {
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
        serverEl.textContent = formatAck(ack);
      } catch (err) {
        serverEl.textContent = `Send failed: ${(err as Error).message}`;
      } finally {
        btn.disabled = false;
      }
    });
}

/** Render the backend ack as a human-readable summary. The
 * content_diff section is what makes the comparison useful — the
 * counter math is already in the JSON, but the diff needs to be
 * surfaced clearly so the operator can tell at a glance whether
 * frontend and backend agree on what's on the pane. */
function formatAck(ack: DebugSnapshotAck): string {
  const lines: string[] = [];
  if (ack.backend && Object.keys(ack.backend).length) {
    lines.push("─── backend counters ───");
    for (const [k, v] of Object.entries(ack.backend)) lines.push(`  ${k}: ${v}`);
  } else {
    lines.push("backend counters: no active WS for this session");
  }
  const d = ack.content_diff;
  if (!d) {
    lines.push("");
    lines.push("─── content diff ───");
    lines.push("  unavailable (no tail to compare or capture-pane failed)");
    return lines.join("\n");
  }
  lines.push("");
  lines.push(`─── content diff (active: ${d.active_type}) ───`);
  for (const [label, sub] of [
    ["normal screen", d.normal] as const,
    ["alternate screen", d.alternate] as const,
  ]) {
    if (!sub) {
      lines.push(`  ${label}: not captured`);
      continue;
    }
    const pct = sub.lines_compared
      ? Math.round((sub.matches / sub.lines_compared) * 1000) / 10
      : 0;
    lines.push(`  ${label}: ${sub.matches}/${sub.lines_compared} match (${pct}%)`
               + `  · frontend=${sub.frontend_lines}L backend=${sub.backend_lines}L`);
    if (sub.mismatch_examples.length) {
      lines.push("    first mismatches (line# counted from bottom):");
      for (const ex of sub.mismatch_examples) {
        lines.push(`      [${ex.line_from_bottom}]`);
        lines.push(`        front: ${truncate(ex.frontend, 100)}`);
        lines.push(`        back:  ${truncate(ex.backend, 100)}`);
      }
    } else if (sub.mismatches === 0 && sub.lines_compared > 0) {
      lines.push("    all compared lines agree.");
    }
  }
  return lines.join("\n");
}

function truncate(s: string, n: number): string {
  if (s.length <= n) return s;
  return s.slice(0, n - 1) + "…";
}

function escapeText(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
function escapeAttr(s: string): string {
  return escapeText(s).replace(/"/g, "&quot;");
}
