// Claude Code conversation-history view (/history?session=<name>).
//
// Console-style, markdown-rendered transcript review:
//   - lazy-pages OLDER blocks as you scroll toward the top (scroll pinned to a
//     stable node so it doesn't jump),
//   - LIVE-TAILS new blocks (replies + the commands/edits claude runs) by
//     polling the `after` cursor and appending while you're at the bottom;
//     when you're scrolled up it just counts them ("N new ↓") and catches up
//     when you return,
//   - keeps a bounded DOM window so a 10k-block transcript can't blow up memory,
//   - reloads from the tail if the bound transcript changes (claude restart).
import "./history.css";
import { renderMarkdown } from "./md-chat";

interface Block { i: number; role: string; text: string; ts: string | null; }
interface Page {
  gen: string;
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
const PAGE_MAX = 500;       // server's per-request cap
const MAX_BLOCKS = 500;     // DOM window cap (M3)

let gen = "";
let oldestCursor = 0;       // index of the OLDEST block currently in the DOM
let newestCursor = -1;      // index of the NEWEST block currently in the DOM
let hasOlder = true;
let total = 0;
let loadingOlder = false;
let polling = false;
let pollFails = 0;

function histUrl(q: Record<string, string | number>): string {
  const u = new URL(`/api/sessions/${encodeURIComponent(session)}/history`, location.origin);
  for (const [k, v] of Object.entries(q)) u.searchParams.set(k, String(v));
  return u.toString();
}
async function fetchPage(q: Record<string, string | number>): Promise<Page> {
  const r = await fetch(histUrl(q), { credentials: "same-origin" });
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
  const who = document.createElement("span"); who.textContent = ROLE_LABEL[b.role] || b.role;
  const ts = document.createElement("span"); ts.className = "hist-block__ts"; ts.textContent = fmtTs(b.ts);
  head.append(who, ts);
  let body: HTMLElement;
  if (b.role === "tool") {
    body = document.createElement("pre");
    body.className = "hist-block__body hist-block__body--tool";
    body.textContent = b.text;          // verbatim, never markup
  } else {
    body = document.createElement("div");
    body.className = "hist-block__body hist-md";
    body.appendChild(renderMarkdown(b.text));   // sanitised fragment, no innerHTML
  }
  el.append(head, body);
  return el;
}
function fragmentFor(blocks: Block[]): DocumentFragment {
  const frag = document.createDocumentFragment();
  for (const b of blocks) frag.appendChild(renderBlock(b));
  return frag;
}

const blockEls = () => doc.querySelectorAll<HTMLElement>(".hist-block");
function firstI(): number { const e = doc.querySelector<HTMLElement>(".hist-block"); return e ? Number(e.dataset.i) : 0; }
function lastI(): number { const els = blockEls(); const e = els[els.length - 1]; return e ? Number(e.dataset.i) : -1; }
function atBottom(): boolean { return doc.scrollTop + doc.clientHeight >= doc.scrollHeight - 40; }

function updateMeta(): void {
  const newer = Math.max(0, total - 1 - newestCursor);
  let s = `${total} block${total === 1 ? "" : "s"}`;
  if (pollFails >= 2) s += " · reconnecting…";
  else if (newer > 0 && !atBottom()) s += ` · ${newer} new ↓`;
  else s += " · live";
  metaEl.textContent = s;
}

// Bound the DOM window (M3): drop from the end the reader is furthest from and
// follow the cursor to the new extreme so paging stays consistent.
function enforceCapTop(): void {
  const els = Array.from(blockEls());
  if (els.length <= MAX_BLOCKS) return;
  for (let k = 0; k < els.length - MAX_BLOCKS; k++) els[k].remove();
  oldestCursor = firstI();
  hasOlder = true;
}
function enforceCapBottom(): void {
  const els = Array.from(blockEls());
  if (els.length <= MAX_BLOCKS) return;
  for (let k = MAX_BLOCKS; k < els.length; k++) els[k].remove();
  newestCursor = lastI();
}

// Append a (possibly large) batch in chunks so a backlog catch-up can't stall
// the main thread rendering hundreds of markdown subtrees in one frame (L5).
function appendChunked(blocks: Block[]): Promise<void> {
  if (blocks.length <= 40) { doc.appendChild(fragmentFor(blocks)); return Promise.resolve(); }
  return new Promise((resolve) => {
    let i = 0;
    const step = () => {
      doc.appendChild(fragmentFor(blocks.slice(i, i + 30)));
      i += 30;
      if (i < blocks.length) requestAnimationFrame(step); else resolve();
    };
    step();
  });
}

async function loadInitial(): Promise<void> {
  if (!session) { if (statusEl) statusEl.textContent = "No session specified."; return; }
  try {
    const page = await fetchPage({ limit: PAGE });
    gen = page.gen; total = page.total; pollFails = 0;
    loadingOlder = false;   // HR2: any in-flight loadOlder now bails on the gen change
    doc.replaceChildren();
    if (!page.blocks.length) {
      const s = document.createElement("div"); s.className = "hist-status";
      s.textContent = "No Claude conversation is bound to this session yet.";
      doc.appendChild(s);
      oldestCursor = 0; newestCursor = -1; hasOlder = false;
    } else {
      doc.appendChild(fragmentFor(page.blocks));
      oldestCursor = firstI(); newestCursor = lastI(); hasOlder = page.hasOlder;
      doc.scrollTop = doc.scrollHeight;
    }
    updateMeta();
    startPolling();
  } catch (e) {
    if (statusEl) {
      const msg = (e as Error).message;
      statusEl.textContent = msg.includes("404")
        ? "No Claude conversation is bound to this session yet."
        : `Couldn't load history: ${msg}`;
    }
  }
}

async function loadOlder(): Promise<void> {
  if (loadingOlder || !hasOlder || oldestCursor <= 0) return;
  loadingOlder = true;
  const g = gen;   // HR2: if the transcript rebinds (loadInitial) during the
                   // await, discard this page rather than prepend stale blocks.
  doc.querySelectorAll(".hist-loading").forEach((n) => n.remove());     // L10: no stacking
  const note = document.createElement("div");
  note.className = "hist-loading"; note.textContent = "loading older…";
  doc.insertBefore(note, doc.firstChild);
  try {
    const page = await fetchPage({ limit: PAGE, before: oldestCursor });
    if (g !== gen) { note.remove(); return; }
    note.remove();
    // M2: pin to a stable node, not total height (immune to any concurrent change).
    const anchor = doc.querySelector<HTMLElement>(".hist-block");
    const beforeTop = anchor ? anchor.getBoundingClientRect().top : 0;
    doc.insertBefore(fragmentFor(page.blocks), doc.firstChild);
    oldestCursor = firstI(); hasOlder = page.hasOlder;
    if (anchor) doc.scrollTop += anchor.getBoundingClientRect().top - beforeTop;
    enforceCapBottom();
  } catch {
    note.textContent = "couldn't load older — scroll to retry";
  } finally {
    loadingOlder = false;
  }
}

async function poll(): Promise<void> {
  if (polling || document.hidden) return;
  polling = true;
  try {
    let page = await fetchPage({ after: newestCursor, limit: PAGE_MAX });
    pollFails = 0;
    // L1: transcript rebound (claude restart). HR1: hold the `polling` guard
    // across the reload (the finally clears it) so the interval can't start a
    // second poll mid-reload.
    if (page.gen !== gen) { await loadInitial(); return; }
    total = page.total;
    // Only render onto the live tail when the reader is there; otherwise just
    // count (avoids yanking them and bounds work while scrolled up).
    if (page.blocks.length && atBottom()) {
      doc.querySelector(".hist-status")?.remove();   // HR3: clear empty-state on first content
      await appendChunked(page.blocks);
      newestCursor = lastI();
      enforceCapTop();
      doc.scrollTop = doc.scrollHeight;
      // L2: keep draining if the server capped the batch (long backlog).
      let guard = 0;
      while (page.hasNewer && atBottom() && guard++ < 20) {
        page = await fetchPage({ after: newestCursor, limit: PAGE_MAX });
        if (page.gen !== gen || !page.blocks.length) break;
        total = page.total;
        await appendChunked(page.blocks);
        newestCursor = lastI();
        enforceCapTop();
        doc.scrollTop = doc.scrollHeight;
      }
    }
    updateMeta();
  } catch {
    pollFails++; updateMeta();   // L9: surface "reconnecting…" instead of stale "live"
  } finally {
    polling = false;
  }
}

let pollTimer: number | null = null;
function startPolling(): void {
  if (pollTimer !== null) return;
  pollTimer = window.setInterval(() => void poll(), POLL_MS);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) void poll(); });
}

doc.addEventListener("scroll", () => {
  if (doc.scrollTop < 240) void loadOlder();
  else if (atBottom() && total - 1 > newestCursor) void poll();   // catch up on return to tail
  updateMeta();
}, { passive: true });

void loadInitial();
