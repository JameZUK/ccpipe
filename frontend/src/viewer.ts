// Standalone rendered-Markdown viewer. Loaded only on /view?path=<abs>
// (served by routes/static.py with its own CSP). Fetches the file via the
// authenticated /api/fs/read, renders GitHub-flavoured Markdown with
// syntax highlighting, KaTeX math and Mermaid diagrams, sanitises the
// result with DOMPurify, and rewrites relative image/link references to
// the matching /api/fs endpoints.
//
// LIVE UPDATES: the viewer polls /api/fs/stat and, whenever the file
// changes on disk (editor save, `claude`, anything), re-renders in place.
// The re-render preserves scroll position by anchoring on the nearest
// heading above the fold, so appends/edits don't jump the page. Mermaid
// loads lazily — only when a document actually contains a diagram.

import MarkdownIt from "markdown-it";
import anchor from "markdown-it-anchor";
import taskLists from "markdown-it-task-lists";
import texmath from "markdown-it-texmath";
import katex from "katex";
import hljs from "highlight.js/lib/common";
import DOMPurify from "dompurify";

import "highlight.js/styles/github-dark.css";
import "katex/dist/katex.min.css";
import "./viewer.css";

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

const md = new MarkdownIt({
  html: true,        // raw HTML is allowed through, then DOMPurify-sanitised
  linkify: true,
  typographer: true,
  highlight: (str, lang): string => {
    // Mermaid fenced blocks are left as a tagged <pre> for post-render
    // diagram rendering (see renderMermaid). Everything else goes through
    // highlight.js when the language is known, else escaped verbatim.
    if (lang === "mermaid") {
      return `<pre class="mermaid">${escapeHtml(str)}</pre>`;
    }
    if (lang && hljs.getLanguage(lang)) {
      try {
        const out = hljs.highlight(str, { language: lang }).value;
        return `<pre class="hljs"><code class="language-${lang}">${out}</code></pre>`;
      } catch { /* fall through to escaped */ }
    }
    return `<pre class="hljs"><code>${escapeHtml(str)}</code></pre>`;
  },
});

md.use(anchor, {
  permalink: anchor.permalink.headerLink(),
  slugify: (s: string) =>
    s.toLowerCase().trim().replace(/[^\w\s-]/g, "").replace(/\s+/g, "-"),
});
md.use(taskLists, { label: true });
md.use(texmath, {
  engine: katex,
  delimiters: "dollars",
  katexOptions: { throwOnError: false },
});

// ── path helpers ────────────────────────────────────────────────────────
function dirOf(absPath: string): string {
  const i = absPath.lastIndexOf("/");
  return i <= 0 ? "/" : absPath.slice(0, i);
}
function baseOf(absPath: string): string {
  return absPath.slice(absPath.lastIndexOf("/") + 1) || absPath;
}
/** A URL that should resolve against the document's own directory:
 *  not a scheme (http:, data:, mailto:…), not root-absolute, not a
 *  pure fragment. */
function isLocalRelative(u: string): boolean {
  return !!u && !/^[a-z][a-z0-9+.-]*:/i.test(u) && !u.startsWith("/") && !u.startsWith("#");
}
/** Resolve a relative reference against an absolute base directory,
 *  collapsing . and .. — dropping any ?query/#fragment first. */
function resolveRelative(baseDir: string, rel: string): string {
  const clean = rel.replace(/[?#].*$/, "");
  const stack = baseDir.split("/").filter(Boolean);
  for (const part of clean.split("/")) {
    if (part === "" || part === ".") continue;
    if (part === "..") stack.pop();
    else stack.push(part);
  }
  return "/" + stack.join("/");
}

// ── DOM targets ─────────────────────────────────────────────────────────
const docEl = document.getElementById("md-doc") as HTMLElement;
const statusEl = document.getElementById("md-status");
const nameEl = document.getElementById("md-name");
const liveEl = document.getElementById("md-live");
const downloadEl = document.getElementById("md-download") as HTMLAnchorElement | null;

// Resolved once from the query string. `root` scopes the document
// switcher to a project directory; it falls back to the file's own
// directory so a viewer opened without one still lists nearby docs.
const _params = new URLSearchParams(location.search);
const filePath = _params.get("path") ?? "";
const baseDir = dirOf(filePath);
const rootDir = _params.get("root") || baseDir;

/** /view URL for *absPath*, preserving the project root so the switcher
 *  stays scoped as the reader navigates between documents. */
function mdViewUrl(absPath: string): string {
  return `/view?path=${encodeURIComponent(absPath)}&root=${encodeURIComponent(rootDir)}`;
}

function fail(message: string): void {
  if (statusEl) {
    statusEl.textContent = message;
    statusEl.classList.add("md-status--error");
  }
}

// ── render ───────────────────────────────────────────────────────────────
function renderSource(source: string): void {
  const dirty = md.render(source);
  // RETURN_DOM_FRAGMENT gives us a sanitised DocumentFragment to append
  // directly — no innerHTML. Defaults strip <script>/event-handlers while
  // keeping HTML, SVG and MathML (the latter needed for KaTeX output).
  const frag = DOMPurify.sanitize(dirty, {
    ADD_ATTR: ["align", "target", "rel"],
    RETURN_DOM_FRAGMENT: true,
  }) as unknown as DocumentFragment;
  docEl.replaceChildren(frag);

  // Rewrite relative references to the doc's own directory.
  docEl.querySelectorAll("img").forEach((img) => {
    const src = img.getAttribute("src") ?? "";
    if (isLocalRelative(src)) {
      img.setAttribute(
        "src",
        `/api/fs/raw?path=${encodeURIComponent(resolveRelative(baseDir, src))}`,
      );
      img.loading = "lazy";
    }
  });
  docEl.querySelectorAll("a[href]").forEach((a) => {
    const href = a.getAttribute("href") ?? "";
    if (isLocalRelative(href)) {
      const target = resolveRelative(baseDir, href);
      if (/\.(md|markdown)$/i.test(target.replace(/[?#].*$/, ""))) {
        a.setAttribute("href", mdViewUrl(target));
      } else {
        a.setAttribute("href", `/api/fs/download?path=${encodeURIComponent(target)}`);
      }
    } else if (/^https?:\/\//i.test(href)) {
      a.setAttribute("target", "_blank");
      a.setAttribute("rel", "noopener noreferrer");
    }
  });

  const mermaidNodes = Array.from(
    docEl.querySelectorAll<HTMLElement>("pre.mermaid"),
  );
  if (mermaidNodes.length) void renderMermaid(mermaidNodes);
}

async function renderMermaid(nodes: HTMLElement[]): Promise<void> {
  try {
    const mermaid = (await import("mermaid")).default;
    mermaid.initialize({
      startOnLoad: false,
      theme: "dark",
      securityLevel: "strict",
      fontFamily: "inherit",
    });
    await mermaid.run({ nodes });
  } catch {
    for (const n of nodes) n.classList.add("mermaid--failed");
  }
}

// ── scroll preservation (anchor on nearest heading above the fold) ───────
interface ScrollAnchor { id: string | null; offset: number; }

function captureScroll(): ScrollAnchor {
  const headings = docEl.querySelectorAll<HTMLElement>(
    "h1[id],h2[id],h3[id],h4[id],h5[id],h6[id]",
  );
  let chosen: HTMLElement | null = null;
  for (const h of headings) {
    // Last heading whose top is at or above the viewport top is the one
    // the reader is "under"; keep its on-screen offset stable.
    if (h.getBoundingClientRect().top <= 1) chosen = h;
    else break;
  }
  if (chosen) return { id: chosen.id, offset: chosen.getBoundingClientRect().top };
  return { id: null, offset: window.scrollY };
}

function restoreScroll(a: ScrollAnchor): void {
  if (a.id) {
    const el = docEl.querySelector<HTMLElement>(`[id="${CSS.escape(a.id)}"]`);
    if (el) {
      // Shift the page so the anchored heading sits at the same on-screen
      // offset it did before the re-render — appends/edits elsewhere
      // don't move the reader.
      window.scrollBy(0, el.getBoundingClientRect().top - a.offset);
      return;
    }
  }
  window.scrollTo(0, a.offset);
}

// ── live polling ─────────────────────────────────────────────────────────
const POLL_MS = 1200;
let lastKey = "";          // `${mtime}:${size}` of the rendered content
let inFlight = false;      // guard against overlapping polls
let removedShown = false;

function setLive(state: "live" | "removed" | "off"): void {
  if (!liveEl) return;
  liveEl.dataset.state = state;
  liveEl.hidden = state === "off";
  liveEl.textContent = state === "removed" ? "file removed" : "live";
}
function pulseLive(): void {
  if (!liveEl) return;
  liveEl.classList.remove("md-bar__live--pulse");
  // force reflow so re-adding the class restarts the animation
  void liveEl.offsetWidth;
  liveEl.classList.add("md-bar__live--pulse");
}

async function fetchJson(url: string): Promise<Response> {
  return fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } });
}

/** One poll tick: cheap stat, and only on a real change re-fetch + re-render. */
async function poll(): Promise<void> {
  if (inFlight || document.visibilityState !== "visible") return;
  inFlight = true;
  try {
    const sr = await fetchJson(`/api/fs/stat?path=${encodeURIComponent(filePath)}`);
    if (sr.status === 404) {
      if (!removedShown) { setLive("removed"); removedShown = true; }
      return;   // keep polling — an atomic rewrite can briefly 404
    }
    if (!sr.ok) return;
    if (removedShown) { removedShown = false; setLive("live"); }
    const meta = await sr.json();
    const key = `${meta.mtime}:${meta.size}`;
    if (key === lastKey) return;

    const cr = await fetchJson(`/api/fs/read?path=${encodeURIComponent(filePath)}`);
    if (!cr.ok) return;   // grew past cap / transient — retry next tick
    const source = (await cr.json()).content ?? "";

    const anchor = captureScroll();
    renderSource(source);
    restoreScroll(anchor);
    lastKey = key;
    pulseLive();
  } catch {
    /* transient network/JSON error — next tick retries */
  } finally {
    inFlight = false;
  }
}

// ── document switcher (the "docs ▾" dropdown) ────────────────────────────
const docsBtn = document.getElementById("md-docs") as HTMLButtonElement | null;
let docsMenu: HTMLElement | null = null;

function closeDocsMenu(): void {
  docsMenu?.remove();
  docsMenu = null;
  document.removeEventListener("pointerdown", onDocsAway, true);
}
function onDocsAway(e: Event): void {
  const t = e.target as Node;
  if (docsMenu && !docsMenu.contains(t) && docsBtn && !docsBtn.contains(t)) closeDocsMenu();
}
/** Keep a right-anchored dropdown on-screen: once populated it may be
 *  wider than the space left of its anchor button (the bug on narrow
 *  phones), so flip it to left-anchored if its left edge spills off. */
function clampMenu(menu: HTMLElement): void {
  const margin = 8;
  const r = menu.getBoundingClientRect();
  if (r.left < margin) {
    menu.style.right = "auto";
    menu.style.left = `${margin}px`;
  } else if (r.right > window.innerWidth - margin) {
    menu.style.right = `${margin}px`;
  }
}

function setupDocsMenu(): void {
  if (!docsBtn) return;
  docsBtn.hidden = false;
  docsBtn.addEventListener("click", async (e) => {
    e.stopPropagation();
    if (docsMenu) { closeDocsMenu(); return; }
    const menu = document.createElement("div");
    menu.className = "md-docs-menu";
    const r = docsBtn.getBoundingClientRect();
    menu.style.top = `${Math.round(r.bottom + 6)}px`;
    menu.style.right = `${Math.round(window.innerWidth - r.right)}px`;
    const note = document.createElement("div");
    note.className = "md-docs-menu__note";
    note.textContent = "Loading…";
    menu.append(note);
    document.body.append(menu);
    docsMenu = menu;
    document.addEventListener("pointerdown", onDocsAway, true);
    try {
      const res = await fetchJson(`/api/fs/markdown-index?root=${encodeURIComponent(rootDir)}`);
      if (docsMenu !== menu) return;
      if (!res.ok) { note.textContent = "Couldn't list documents."; return; }
      const data = await res.json();
      menu.replaceChildren();
      if (!data.entries.length) {
        const empty = document.createElement("div");
        empty.className = "md-docs-menu__note";
        empty.textContent = "No Markdown files found.";
        menu.append(empty);
        clampMenu(menu);
        return;
      }
      for (const ent of data.entries as { name: string; path: string; rel: string }[]) {
        const item = document.createElement("button");
        item.type = "button";
        item.className = "md-docs-menu__item";
        if (ent.path === filePath) item.classList.add("md-docs-menu__item--active");
        item.textContent = ent.rel;
        item.title = ent.rel;
        item.addEventListener("click", () => {
          closeDocsMenu();
          if (ent.path !== filePath) location.assign(mdViewUrl(ent.path));
        });
        menu.append(item);
      }
      if (data.truncated) {
        const trunc = document.createElement("div");
        trunc.className = "md-docs-menu__note";
        trunc.textContent = `first ${data.entries.length} shown`;
        menu.append(trunc);
      }
      clampMenu(menu);
    } catch {
      if (docsMenu === menu) note.textContent = "Failed to load.";
    }
  });
}

// ── boot ─────────────────────────────────────────────────────────────────
async function main(): Promise<void> {
  if (!filePath) { fail("No file specified."); return; }

  const name = baseOf(filePath);
  document.title = `${name} · ccpipe`;
  if (nameEl) { nameEl.textContent = name; nameEl.title = filePath; }
  if (downloadEl) {
    downloadEl.href = `/api/fs/download?path=${encodeURIComponent(filePath)}`;
    downloadEl.hidden = false;
  }

  let res: Response;
  try {
    res = await fetchJson(`/api/fs/read?path=${encodeURIComponent(filePath)}`);
  } catch {
    fail("Network error loading the file."); return;
  }
  if (res.status === 401 || res.status === 403) {
    fail("Not authenticated. Open ccpipe in another tab, sign in, then reload this page.");
    return;
  }
  if (res.status === 413) { fail("File is too large to render (1 MiB limit)."); return; }
  if (res.status === 415) { fail("This file isn't UTF-8 text."); return; }
  if (!res.ok) { fail(`Could not load file (HTTP ${res.status}).`); return; }

  let body: { content?: string; mtime?: number; size?: number };
  try { body = await res.json(); } catch { fail("Malformed response from server."); return; }

  statusEl?.remove();
  renderSource(body.content ?? "");
  setupDocsMenu();

  // Seed the change key from a stat call so its representation matches the
  // poll's (read returns an int mtime; stat a float — comparing the two
  // would re-render forever). One cheap request, then steady-state polling.
  try {
    const sr = await fetchJson(`/api/fs/stat?path=${encodeURIComponent(filePath)}`);
    if (sr.ok) { const m = await sr.json(); lastKey = `${m.mtime}:${m.size}`; }
  } catch { /* polling will settle it */ }

  setLive("live");
  window.setInterval(() => { void poll(); }, POLL_MS);
  // Re-check immediately when the tab regains focus (polling pauses while
  // hidden), so a doc edited in the background updates the moment you look.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") void poll();
  });
}

void main();
