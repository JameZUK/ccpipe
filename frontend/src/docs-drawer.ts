// Document drawer for the Markdown viewer (/view). The toolbar "docs"
// button opens a side panel over every Markdown file under the project
// root (one /api/fs/markdown-index fetch, searched + treed client-side):
//
//   - empty query  → "Recent" (last docs opened under this root),
//                    "Recently modified" (newest mtimes on disk) + a
//                    folder tree, expanded along the current doc's path,
//                    single-child folder chains compacted ("a/b/c"),
//                    expansion remembered per root.
//   - typed query  → flat ranked results. Every space-separated token must
//                    appear in the relative path; filename hits outrank
//                    directory hits; matches are highlighted.
//
// Wide screens (≥960px) dock the drawer beside the document and remember
// whether it was left open (picking a doc replaces the page); narrow screens
// get a full-height overlay sheet. "/" opens it and focuses the search;
// arrows / Enter / ←→ drive the list.

import { MODIFIED_SHOW, RECENT_SHOW, fmtAge, readRecents, recentlyModified, recordRecent } from "./doc-recents";

interface DocEntry { path: string; rel: string; mtime: number }

interface DirNode {
  name: string;   // display label — may be a compacted "a/b/c" chain
  rel: string;    // rel path of the deepest dir in the chain (expansion key)
  dirs: DirNode[];
  files: DocEntry[];
  count: number;  // Markdown files anywhere below
}

export interface DocsDrawerOptions {
  button: HTMLButtonElement;
  rootDir: string;
  filePath: string;
  viewUrl: (absPath: string) => string;
}

const WIDE_QUERY = "(min-width: 960px)";
// Re-fetch the index when the drawer reopens after this long, so
// "Recently modified" keeps up with files being edited in the background.
const INDEX_STALE_MS = 30_000;
const RESULT_MAX = 200;
const LS_OPEN = "ccpipe.viewer.drawerOpen";
const lsExpandedKey = (root: string) => `ccpipe.viewer.expanded:${root}`;

// localStorage can be absent or throw (private windows, blocked storage);
// every access degrades to "nothing remembered".
function lsGet<T>(key: string, fallback: T): T {
  try {
    const v = localStorage.getItem(key);
    return v === null ? fallback : (JSON.parse(v) as T);
  } catch { return fallback; }
}
function lsSet(key: string, val: unknown): void {
  try { localStorage.setItem(key, JSON.stringify(val)); } catch { /* ignore */ }
}

const baseName = (rel: string) => rel.slice(rel.lastIndexOf("/") + 1);
const dirName = (rel: string) => { const i = rel.lastIndexOf("/"); return i < 0 ? "" : rel.slice(0, i); };
const cmpText = (a: string, b: string) => a.localeCompare(b, undefined, { sensitivity: "base", numeric: true });

function buildTree(entries: DocEntry[]): DirNode {
  const root: DirNode = { name: "", rel: "", dirs: [], files: [], count: 0 };
  const byRel = new Map<string, DirNode>([["", root]]);
  for (const e of entries) {
    const parts = e.rel.split("/");
    let node = root;
    let rel = "";
    for (let i = 0; i < parts.length - 1; i++) {
      rel = rel ? `${rel}/${parts[i]}` : parts[i];
      let child = byRel.get(rel);
      if (!child) {
        child = { name: parts[i], rel, dirs: [], files: [], count: 0 };
        byRel.set(rel, child);
        node.dirs.push(child);
      }
      node = child;
    }
    node.files.push(e);
  }
  const finish = (n: DirNode): number => {
    n.count = n.files.length;
    for (const d of n.dirs) n.count += finish(d);
    // Compact single-child, file-less chains into one "a/b/c" row. The
    // merged node keeps the deepest dir's rel as its expansion key.
    n.dirs = n.dirs.map((d) => {
      while (d.files.length === 0 && d.dirs.length === 1) {
        const c = d.dirs[0];
        d = { ...c, name: `${d.name}/${c.name}` };
      }
      return d;
    });
    n.dirs.sort((a, b) => cmpText(a.name, b.name));
    n.files.sort((a, b) => cmpText(baseName(a.rel), baseName(b.rel)));
    return n.count;
  };
  finish(root);
  return root;
}

/** Rank *e* against *tokens* (lower-cased); null = not every token matched. */
function scoreEntry(e: DocEntry, tokens: string[]): number | null {
  const rel = e.rel.toLowerCase();
  const name = baseName(rel);
  let s = 0;
  for (const t of tokens) {
    if (!rel.includes(t)) return null;
    s += name.startsWith(t) ? 15 : name.includes(t) ? 10 : 1;
  }
  return s;
}

/** Fill *el* with *text*, wrapping every token occurrence in <mark>.
 *  Text nodes only — file names never reach an HTML parser. */
function highlightInto(el: HTMLElement, text: string, tokens: string[]): void {
  const lower = text.toLowerCase();
  const spans: [number, number][] = [];
  for (const t of tokens) {
    for (let i = lower.indexOf(t); i !== -1; i = lower.indexOf(t, i + t.length)) {
      spans.push([i, i + t.length]);
    }
  }
  spans.sort((a, b) => a[0] - b[0]);
  let pos = 0;
  for (const [s0, e0] of spans) {
    if (e0 <= pos) continue;
    const s = Math.max(s0, pos);
    if (s > pos) el.append(text.slice(pos, s));
    // Extend over overlapping/adjacent spans so marks never nest.
    let e = e0;
    for (const [s1, e1] of spans) if (s1 <= e && e1 > e) e = e1;
    const m = document.createElement("mark");
    m.textContent = text.slice(s, e);
    el.append(m);
    pos = e;
  }
  if (pos < text.length) el.append(text.slice(pos));
}

export function setupDocsDrawer(opts: DocsDrawerOptions): void {
  const { button, rootDir, filePath, viewUrl } = opts;
  const wideMq = window.matchMedia(WIDE_QUERY);
  const coarse = window.matchMedia("(pointer: coarse)").matches;

  // Record this visit before anything renders, so "Recent" is up to date
  // on the very page that opened it.
  recordRecent(rootDir, filePath);

  const expanded = new Set(lsGet<string[]>(lsExpandedKey(rootDir), []));

  // ── DOM ──────────────────────────────────────────────────────────────
  const drawer = document.createElement("aside");
  drawer.className = "md-drawer";
  drawer.hidden = true;
  drawer.setAttribute("aria-label", "Documents");

  const head = document.createElement("div");
  head.className = "md-drawer__head";
  const title = document.createElement("span");
  title.className = "md-drawer__title";
  title.textContent = "Documents";
  const countEl = document.createElement("span");
  countEl.className = "md-drawer__count";
  const closeBtn = document.createElement("button");
  closeBtn.type = "button";
  closeBtn.className = "md-drawer__close";
  closeBtn.title = "Close (Esc)";
  closeBtn.setAttribute("aria-label", "Close documents");
  closeBtn.textContent = "×";
  head.append(title, countEl, closeBtn);

  const search = document.createElement("input");
  search.type = "search";
  search.className = "md-drawer__search";
  search.placeholder = "Search documents…";
  search.setAttribute("aria-label", "Search documents");
  search.autocomplete = "off";
  search.spellcheck = false;

  const list = document.createElement("div");
  list.className = "md-drawer__list";
  list.setAttribute("role", "tree");

  drawer.append(head, search, list);

  const backdrop = document.createElement("div");
  backdrop.className = "md-drawer-backdrop";
  backdrop.hidden = true;

  document.body.append(backdrop, drawer);

  // ── data ─────────────────────────────────────────────────────────────
  let entries: DocEntry[] | null = null;
  let tree: DirNode | null = null;
  let truncated = false;
  let loadError: string | null = null;
  let loading: Promise<void> | null = null;
  let loadedAt = 0;

  const load = (): Promise<void> => {
    if (loading && loadedAt && Date.now() - loadedAt > INDEX_STALE_MS) loading = null;
    loading ??= (async () => {
      try {
        loadError = null;
        const res = await fetch(`/api/fs/markdown-index?root=${encodeURIComponent(rootDir)}`, {
          credentials: "same-origin", headers: { Accept: "application/json" },
        });
        if (!res.ok) { loadError = "Couldn't list documents."; return; }
        const data = await res.json() as { entries: DocEntry[]; truncated?: boolean };
        entries = data.entries.map((e) => ({ path: e.path, rel: e.rel, mtime: e.mtime ?? 0 }));
        loadedAt = Date.now();
        truncated = !!data.truncated;
        tree = buildTree(entries);
        // Always reveal the current doc's folder path.
        const cur = entries.find((e) => e.path === filePath);
        if (cur) {
          const parts = cur.rel.split("/").slice(0, -1);
          for (let i = 1; i <= parts.length; i++) expanded.add(parts.slice(0, i).join("/"));
        }
        countEl.textContent = String(entries.length);
      } catch {
        loadError = "Failed to load.";
      }
    })();
    return loading;
  };

  // ── rendering ────────────────────────────────────────────────────────
  const note = (text: string) => {
    const n = document.createElement("div");
    n.className = "md-drawer__note";
    n.textContent = text;
    return n;
  };
  const section = (text: string) => {
    const h = document.createElement("div");
    h.className = "md-drawer__section";
    h.textContent = text;
    return h;
  };

  /** A two-line row: filename, dimmed directory underneath. */
  const flatRow = (e: DocEntry, tokens: string[], showAge = false): HTMLElement => {
    const row = document.createElement("div");
    row.className = "md-drawer__row md-drawer__row--flat";
    row.setAttribute("role", "treeitem");
    row.tabIndex = -1;
    row.dataset.kind = "file";
    row.dataset.path = e.path;
    row.dataset.depth = "0";
    row.title = e.rel;
    if (e.path === filePath) row.classList.add("md-drawer__row--active");
    const n = document.createElement("span");
    n.className = "md-drawer__name";
    highlightInto(n, baseName(e.rel), tokens);
    if (showAge && e.mtime) {
      const line = document.createElement("span");
      line.className = "md-drawer__line";
      const age = document.createElement("span");
      age.className = "md-drawer__badge";
      age.textContent = fmtAge(e.mtime);
      age.title = new Date(e.mtime * 1000).toLocaleString();
      line.append(n, age);
      row.append(line);
    } else {
      row.append(n);
    }
    const dir = dirName(e.rel);
    if (dir) {
      const d = document.createElement("span");
      d.className = "md-drawer__dir";
      highlightInto(d, dir, tokens);
      row.append(d);
    }
    return row;
  };

  const treeRow = (label: string, depth: number): HTMLElement => {
    const row = document.createElement("div");
    row.className = "md-drawer__row";
    row.setAttribute("role", "treeitem");
    row.tabIndex = -1;
    row.dataset.depth = String(depth);
    row.style.setProperty("--depth", String(depth));
    const caret = document.createElement("span");
    caret.className = "md-drawer__caret";
    const n = document.createElement("span");
    n.className = "md-drawer__name";
    n.textContent = label;
    row.append(caret, n);
    return row;
  };

  const renderDir = (node: DirNode, depth: number, out: HTMLElement[]): void => {
    for (const d of node.dirs) {
      const open = expanded.has(d.rel);
      const row = treeRow(d.name, depth);
      row.dataset.kind = "dir";
      row.dataset.rel = d.rel;
      row.setAttribute("aria-expanded", String(open));
      row.title = d.rel;
      (row.firstChild as HTMLElement).textContent = open ? "▾" : "▸";
      const c = document.createElement("span");
      c.className = "md-drawer__badge";
      c.textContent = String(d.count);
      row.append(c);
      out.push(row);
      if (open) renderDir(d, depth + 1, out);
    }
    for (const f of node.files) {
      const row = treeRow(baseName(f.rel), depth);
      row.dataset.kind = "file";
      row.dataset.path = f.path;
      row.title = f.rel;
      if (f.path === filePath) row.classList.add("md-drawer__row--active");
      out.push(row);
    }
  };

  const tokensOf = (q: string) => q.toLowerCase().split(/\s+/).filter(Boolean);

  const render = (): void => {
    const keepScroll = list.scrollTop;
    list.replaceChildren();
    if (loadError) { list.append(note(loadError)); return; }
    if (!entries || !tree) { list.append(note("Loading…")); return; }
    if (!entries.length) { list.append(note("No Markdown files found.")); return; }

    const tokens = tokensOf(search.value);
    if (tokens.length) {
      const hits: { e: DocEntry; s: number }[] = [];
      for (const e of entries) {
        const s = scoreEntry(e, tokens);
        if (s !== null) hits.push({ e, s });
      }
      hits.sort((a, b) => b.s - a.s || a.e.rel.length - b.e.rel.length || cmpText(a.e.rel, b.e.rel));
      if (!hits.length) { list.append(note("No matches.")); return; }
      for (const h of hits.slice(0, RESULT_MAX)) list.append(flatRow(h.e, tokens));
      if (hits.length > RESULT_MAX) list.append(note(`${hits.length - RESULT_MAX} more — refine the search`));
      list.scrollTop = 0;
      return;
    }

    const known = new Map(entries.map((e) => [e.path, e]));
    const recent = readRecents(rootDir)
      .filter((p) => p !== filePath && known.has(p))
      .slice(0, RECENT_SHOW);
    if (recent.length) {
      list.append(section("Recent"));
      for (const p of recent) list.append(flatRow(known.get(p)!, []));
    }
    const modified = recentlyModified(entries, MODIFIED_SHOW);
    if (modified.length) {
      list.append(section("Recently modified"));
      for (const e of modified) list.append(flatRow(e, [], true));
    }
    if (recent.length || modified.length) list.append(section("All documents"));
    const rows: HTMLElement[] = [];
    renderDir(tree, 0, rows);
    list.append(...rows);
    if (truncated) list.append(note(`first ${entries.length} shown (index capped)`));
    list.scrollTop = keepScroll;
  };

  // ── list interaction ─────────────────────────────────────────────────
  const rows = () => Array.from(list.querySelectorAll<HTMLElement>(".md-drawer__row"));

  const focusRow = (row: HTMLElement | undefined): void => {
    if (!row) return;
    for (const r of rows()) r.tabIndex = -1;
    row.tabIndex = 0;
    row.focus({ preventScroll: true });
    row.scrollIntoView({ block: "nearest" });
  };

  const setExpanded = (rel: string, open: boolean): void => {
    if (open) expanded.add(rel); else expanded.delete(rel);
    lsSet(lsExpandedKey(rootDir), [...expanded]);
    const hadFocus = drawer.contains(document.activeElement);
    render();
    const again = list.querySelector<HTMLElement>(`.md-drawer__row[data-kind="dir"][data-rel="${CSS.escape(rel)}"]`);
    if (again && hadFocus) focusRow(again);
  };

  const activate = (row: HTMLElement): void => {
    if (row.dataset.kind === "dir") {
      setExpanded(row.dataset.rel!, row.getAttribute("aria-expanded") !== "true");
      return;
    }
    const path = row.dataset.path!;
    if (path === filePath) { if (!wideMq.matches) close(); return; }
    // replace, not assign: keeping the viewer's history to one page lets
    // the close button's window.close() work (browsers only allow a
    // script close of a window whose history is a single document).
    location.replace(viewUrl(path));
  };

  list.addEventListener("click", (e) => {
    const row = (e.target as HTMLElement).closest<HTMLElement>(".md-drawer__row");
    if (row) activate(row);
  });

  list.addEventListener("keydown", (e) => {
    const row = (e.target as HTMLElement).closest<HTMLElement>(".md-drawer__row");
    if (!row) return;
    const all = rows();
    const i = all.indexOf(row);
    const isDir = row.dataset.kind === "dir";
    const isOpen = row.getAttribute("aria-expanded") === "true";
    switch (e.key) {
      case "ArrowDown": focusRow(all[i + 1]); break;
      case "ArrowUp": if (i === 0) search.focus(); else focusRow(all[i - 1]); break;
      case "Home": focusRow(all[0]); break;
      case "End": focusRow(all[all.length - 1]); break;
      case "Enter":
      case " ": activate(row); break;
      case "ArrowRight":
        if (isDir && !isOpen) setExpanded(row.dataset.rel!, true);
        else if (isDir) focusRow(all[i + 1]);
        break;
      case "ArrowLeft": {
        if (isDir && isOpen) { setExpanded(row.dataset.rel!, false); break; }
        // Jump to the enclosing folder row.
        const depth = Number(row.dataset.depth);
        for (let j = i - 1; j >= 0; j--) {
          if (all[j].dataset.kind === "dir" && Number(all[j].dataset.depth) < depth) { focusRow(all[j]); break; }
        }
        break;
      }
      case "Escape": search.focus(); break;
      default: return;
    }
    e.preventDefault();
  });

  search.addEventListener("input", render);
  search.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); focusRow(rows()[0]); }
    else if (e.key === "Enter") {
      e.preventDefault();
      const first = list.querySelector<HTMLElement>('.md-drawer__row[data-kind="file"]');
      if (first && search.value.trim()) activate(first);
    } else if (e.key === "Escape") {
      e.preventDefault();
      if (search.value) { search.value = ""; render(); } else close();
    }
  });

  // ── open / close ─────────────────────────────────────────────────────
  const isOpen = () => !drawer.hidden;

  function open(focusSearch: boolean, persist = true): void {
    const wide = wideMq.matches;
    drawer.hidden = false;
    backdrop.hidden = wide;
    document.body.classList.toggle("md-drawer-docked", wide);
    document.body.classList.toggle("md-drawer-overlay", !wide);
    button.setAttribute("aria-expanded", "true");
    if (wide && persist) lsSet(LS_OPEN, true);
    render();
    void load().then(() => {
      if (!isOpen()) return;
      render();
      // "nearest" scrolls only when the current doc's tree row is off-screen,
      // so the Recent / Recently modified sections above stay in view.
      list.querySelector(".md-drawer__row--active:not(.md-drawer__row--flat)")
        ?.scrollIntoView({ block: "nearest" });
    });
    // No auto-focus on touch: it would pop the keyboard over half the tree.
    if (focusSearch && !coarse) search.focus();
  }

  function close(persist = true): void {
    if (!isOpen()) return;
    const hadFocus = drawer.contains(document.activeElement);
    drawer.hidden = true;
    backdrop.hidden = true;
    document.body.classList.remove("md-drawer-docked", "md-drawer-overlay");
    button.setAttribute("aria-expanded", "false");
    if (wideMq.matches && persist) lsSet(LS_OPEN, false);
    if (hadFocus) button.focus();
  }

  button.hidden = false;
  button.setAttribute("aria-expanded", "false");
  button.addEventListener("click", () => { if (isOpen()) close(); else open(true); });
  closeBtn.addEventListener("click", () => close());
  backdrop.addEventListener("click", () => close());

  drawer.addEventListener("keydown", (e) => {
    // Esc anywhere in an overlay closes it (the search box handles its own).
    if (e.key === "Escape" && !e.defaultPrevented && e.target !== search && !wideMq.matches) {
      e.preventDefault();
      close();
    }
  });

  document.addEventListener("keydown", (e) => {
    if (e.key !== "/" || e.ctrlKey || e.metaKey || e.altKey) return;
    const t = e.target as HTMLElement | null;
    if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
    e.preventDefault();
    if (!isOpen()) open(true); else search.focus();
  });

  // Crossing the breakpoint swaps docked ↔ overlay; an overlay appearing
  // on its own would be jarring, so only the docked form reopens itself.
  wideMq.addEventListener("change", () => {
    close(false);
    if (wideMq.matches && lsGet(LS_OPEN, false)) open(false, false);
  });

  if (wideMq.matches && lsGet(LS_OPEN, false)) open(false, false);
}
