// Claude Code conversation-history view (/history?session=<name>).
//
// Console-style monospace rendering of the session's transcript:
//   - lazy-pages OLDER blocks as you scroll toward the top (scroll position
//     pinned so it doesn't jump),
//   - LIVE-TAILS new blocks (replies AND the commands/edits claude runs) by
//     polling the `after` cursor, appending them, and following the bottom
//     when you're already there — no manual refresh.
// This is the review surface; tmux console scrollback is separate.
import "./history.css";
import { renderMarkdown } from "./md-chat";

interface Block { i: number; role: string; text: string; ts: string | null; }
interface Page {
  total: number;
  blocks: Block[];
  oldestCursor: number;
  newestCursor: number;
  hasOlder: boolean;
  hasNewer?: boolean;
}

const params = new URLSearchParams(location.search);
const session = params.get("session") || "";
const doc = document.getElementById("hist-doc") as HTMLElement;
const statusEl = document.getElementById("hist-status");
const nameEl = document.getElementById("hist-name") as HTMLElement;
const metaEl = document.getElementById("hist-meta") as HTMLElement;
nameEl.textContent = session ? `${session} · history` : "history";

const PAGE = 40;
const POLL_MS = 3500;
let oldestCursor = 0;
let newestCursor = -1;
let hasOlder = true;
let total = 0;
let loadingOlder = false;
let polling = false;

function histUrl(params: Record<string, string | number>): string {
  const u = new URL(`/api/sessions/${encodeURIComponent(session)}/history`, location.origin);
  for (const [k, v] of Object.entries(params)) u.searchParams.set(k, String(v));
  return u.toString();
}
async function fetchJson(url: string): Promise<Page> {
  const r = await fetch(url, { credentials: "same-origin" });
  if (!r.ok) throw new Error(`history ${r.status}`);
  return r.json() as Promise<Page>;
}

function fmtTs(ts: string | null): string {
  if (!ts) return "";
  const d = new Date(ts);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleString();
}

const ROLE_CLASS: Record<string, string> = { user: "user", assistant: "claude", tool: "tool" };
const ROLE_LABEL: Record<string, string> = { user: "you", assistant: "claude", tool: "ran" };

function renderBlock(b: Block): HTMLElement {
  const el = document.createElement("section");
  el.className = `hist-block hist-block--${ROLE_CLASS[b.role] || "claude"}`;
  el.dataset.i = String(b.i);

  const head = document.createElement("div");
  head.className = "hist-block__head";
  const who = document.createElement("span");
  who.textContent = ROLE_LABEL[b.role] || b.role;
  const ts = document.createElement("span");
  ts.className = "hist-block__ts";
  ts.textContent = fmtTs(b.ts);
  head.append(who, ts);

  let body: HTMLElement;
  if (b.role === "tool") {
    // Commands and diffs stay verbatim, monospace — not markdown.
    body = document.createElement("pre");
    body.className = "hist-block__body hist-block__body--tool";
    body.textContent = b.text;          // textContent: never interprets markup
  } else {
    // Prose renders as markdown (DOMPurify-sanitised fragment, no innerHTML),
    // like Claude Code's TUI.
    body = document.createElement("div");
    body.className = "hist-block__body hist-md";
    body.appendChild(renderMarkdown(b.text));
  }

  el.append(head, body);
  return el;
}

function fragmentFor(blocks: Block[]): DocumentFragment {
  const frag = document.createDocumentFragment();
  for (const b of blocks) frag.appendChild(renderBlock(b));
  return frag;
}

function atBottom(): boolean {
  return doc.scrollTop + doc.clientHeight >= doc.scrollHeight - 40;
}
function updateMeta(): void {
  metaEl.textContent = `${total} block${total === 1 ? "" : "s"} · live`;
}

async function loadInitial(): Promise<void> {
  if (!session) { if (statusEl) statusEl.textContent = "No session specified."; return; }
  try {
    const page = await fetchJson(histUrl({ limit: PAGE }));
    total = page.total; oldestCursor = page.oldestCursor; hasOlder = page.hasOlder;
    newestCursor = page.newestCursor;
    doc.replaceChildren();
    if (!page.blocks.length) {
      const s = document.createElement("div");
      s.className = "hist-status";
      s.textContent = "No conversation history for this session yet.";
      doc.appendChild(s);
    } else {
      doc.appendChild(fragmentFor(page.blocks));
      doc.scrollTop = doc.scrollHeight;   // newest at the bottom, like a console
    }
    updateMeta();
    startPolling();
  } catch (e) {
    if (statusEl) statusEl.textContent = `Couldn't load history: ${(e as Error).message}`;
  }
}

async function loadOlder(): Promise<void> {
  if (loadingOlder || !hasOlder || oldestCursor <= 0) return;
  loadingOlder = true;
  const note = document.createElement("div");
  note.className = "hist-loading";
  note.textContent = "loading older…";
  doc.insertBefore(note, doc.firstChild);
  try {
    const prevHeight = doc.scrollHeight, prevTop = doc.scrollTop;
    const page = await fetchJson(histUrl({ limit: PAGE, before: oldestCursor }));
    oldestCursor = page.oldestCursor; hasOlder = page.hasOlder;
    note.remove();
    doc.insertBefore(fragmentFor(page.blocks), doc.firstChild);
    doc.scrollTop = prevTop + (doc.scrollHeight - prevHeight);   // pin position
  } catch {
    note.textContent = "couldn't load older — scroll to retry";
  } finally {
    loadingOlder = false;
  }
}

// Live tail: pull anything newer than what we've shown and append it; follow
// the bottom only if the reader is already there (don't yank them mid-scroll).
async function poll(): Promise<void> {
  if (polling || document.hidden) return;
  polling = true;
  try {
    const page = await fetchJson(histUrl({ after: newestCursor, limit: 500 }));
    total = page.total;
    if (page.blocks.length) {
      const wasBottom = atBottom();
      doc.appendChild(fragmentFor(page.blocks));
      newestCursor = page.newestCursor;
      if (wasBottom) doc.scrollTop = doc.scrollHeight;
      updateMeta();
    }
  } catch {
    /* transient — try again next tick */
  } finally {
    polling = false;
  }
}

let pollTimer: number | null = null;
function startPolling(): void {
  if (pollTimer !== null) return;
  pollTimer = window.setInterval(() => void poll(), POLL_MS);
  // Catch up immediately when the tab regains focus.
  document.addEventListener("visibilitychange", () => { if (!document.hidden) void poll(); });
}

doc.addEventListener("scroll", () => {
  if (doc.scrollTop < 240) void loadOlder();
}, { passive: true });

void loadInitial();
