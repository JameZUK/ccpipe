// Claude Code conversation-history view (/history?session=<name>).
//
// Console-style monospace rendering of the session's transcript, lazy-paged:
// loads the newest page and scrolls to the bottom, then fetches and PREPENDS
// older pages as the user scrolls toward the top (pinning scroll position so
// the view doesn't jump). This is the review surface that replaces synthesised
// claude terminal scrollback — tmux console scrollback is separate.
import "./history.css";

interface Block { i: number; role: string; text: string; ts: string | null; }
interface Page { total: number; blocks: Block[]; oldestCursor: number; hasOlder: boolean; }

const params = new URLSearchParams(location.search);
const session = params.get("session") || "";
const doc = document.getElementById("hist-doc") as HTMLElement;
const statusEl = document.getElementById("hist-status");
const nameEl = document.getElementById("hist-name") as HTMLElement;
const metaEl = document.getElementById("hist-meta") as HTMLElement;
nameEl.textContent = session ? `${session} · history` : "history";

const PAGE = 40;
let oldestCursor: number | null = null;
let hasOlder = true;
let total = 0;
let loading = false;

async function fetchPage(before: number | null): Promise<Page> {
  const u = new URL(`/api/sessions/${encodeURIComponent(session)}/history`, location.origin);
  u.searchParams.set("limit", String(PAGE));
  if (before !== null) u.searchParams.set("before", String(before));
  const r = await fetch(u.toString(), { credentials: "same-origin" });
  if (!r.ok) throw new Error(`history ${r.status}`);
  return r.json() as Promise<Page>;
}

function fmtTs(ts: string | null): string {
  if (!ts) return "";
  const d = new Date(ts);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleString();
}

function renderBlock(b: Block): HTMLElement {
  const el = document.createElement("section");
  el.className = `hist-block hist-block--${b.role === "user" ? "user" : "claude"}`;
  el.dataset.i = String(b.i);

  const head = document.createElement("div");
  head.className = "hist-block__head";
  const who = document.createElement("span");
  who.textContent = b.role === "user" ? "you" : "claude";
  const ts = document.createElement("span");
  ts.className = "hist-block__ts";
  ts.textContent = fmtTs(b.ts);
  head.append(who, ts);

  const body = document.createElement("pre");
  body.className = "hist-block__body";
  body.textContent = b.text;            // textContent: never interprets markup

  el.append(head, body);
  return el;
}

function fragmentFor(blocks: Block[]): DocumentFragment {
  const frag = document.createDocumentFragment();
  for (const b of blocks) frag.appendChild(renderBlock(b));
  return frag;
}

async function loadInitial(): Promise<void> {
  if (!session) { if (statusEl) statusEl.textContent = "No session specified."; return; }
  try {
    const page = await fetchPage(null);
    total = page.total; oldestCursor = page.oldestCursor; hasOlder = page.hasOlder;
    doc.replaceChildren();
    if (!page.blocks.length) {
      const s = document.createElement("div");
      s.className = "hist-status";
      s.textContent = "No conversation history for this session yet.";
      doc.appendChild(s);
      return;
    }
    doc.appendChild(fragmentFor(page.blocks));
    metaEl.textContent = `${total} message${total === 1 ? "" : "s"}`;
    doc.scrollTop = doc.scrollHeight;   // newest at the bottom, like a console
  } catch (e) {
    if (statusEl) statusEl.textContent = `Couldn't load history: ${(e as Error).message}`;
  }
}

async function loadOlder(): Promise<void> {
  if (loading || !hasOlder || oldestCursor === null || oldestCursor <= 0) return;
  loading = true;
  const note = document.createElement("div");
  note.className = "hist-loading";
  note.textContent = "loading older…";
  doc.insertBefore(note, doc.firstChild);
  try {
    const prevHeight = doc.scrollHeight, prevTop = doc.scrollTop;
    const page = await fetchPage(oldestCursor);
    oldestCursor = page.oldestCursor; hasOlder = page.hasOlder;
    note.remove();
    doc.insertBefore(fragmentFor(page.blocks), doc.firstChild);
    // Pin the viewport to the same content it was showing before the prepend.
    doc.scrollTop = prevTop + (doc.scrollHeight - prevHeight);
  } catch {
    note.textContent = "couldn't load older — scroll to retry";
  } finally {
    loading = false;
  }
}

doc.addEventListener("scroll", () => {
  if (doc.scrollTop < 240) void loadOlder();
}, { passive: true });

void loadInitial();
