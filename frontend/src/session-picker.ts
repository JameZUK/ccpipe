// The landing page: every tmux session, in one of three views, plus the
// new-session form.
//
// Views (toolbar toggle, remembered per device):
//   compact  — one line each: state, name, task, last activity
//   detailed — a table on wide screens (session + task, directory, activity,
//              last opened, created); stacked lines on a phone
//   grid     — cards with a live preview of the bottom of the screen
// A sort (activity / last opened / created / name) and a filter box apply to
// all three. The list refreshes itself every REFRESH_MS while visible but
// keeps its order until the user re-sorts or comes back to the page, so
// rows never jump under a finger.
//
// "Working" comes from the pane title: Claude Code puts its task there,
// prefixed with a braille spinner while it works and ✳ when idle.

import { apiJson, getFsConfig } from "./api";
import { sparkMark } from "./brand";
import { openDirectoryBrowser } from "./directory-browser";
import { clearLastSession } from "./display-prefs";
import {
  FOLDER_SVG, GEAR_SVG, ICON_DOWNLOAD, ICON_PLUS, ICON_SEARCH, ICON_VIEW_COMPACT,
  ICON_VIEW_DETAILED, ICON_VIEW_GRID, ICON_X, KEBAB_SVG, PIN_SVG, iconNode,
} from "./icons";
import { loadRecentDirs, pushRecentDir } from "./recent-dirs";
// `./settings` is dynamically imported in the gear click handler so it
// stays out of the session-picker chunk.

type SessionInfo = {
  name: string;
  windows: number;
  attached: boolean;
  /** When ccpipe first created it (kept across reboots — session_meta.py). */
  created: number;
  /** In the sticky map: recreated after a backend restart / reboot. */
  sticky: boolean;
  /** Last pane output (tmux #{session_activity}). */
  activity: number;
  /** When a terminal last opened it; 0 = not known. Absent from an older backend. */
  opened?: number;
  /** Terminals / tiles attached right now. */
  clients?: number;
  /** The active pane's directory and title. */
  cwd?: string | null;
  title?: string | null;
};

type ClaudeSession = {
  id: string;
  mtime: number;
  size: number;
  firstUserMessage: string | null;
};

type View = "compact" | "detailed" | "grid";
type Sort = "activity" | "opened" | "created" | "name";
type State = "working" | "open" | "idle";

const VIEWS: ReadonlyArray<{ id: View; label: string; icon: string }> = [
  { id: "compact", label: "Compact list", icon: ICON_VIEW_COMPACT },
  { id: "detailed", label: "Detailed list", icon: ICON_VIEW_DETAILED },
  { id: "grid", label: "Grid with screen previews", icon: ICON_VIEW_GRID },
];
const SORTS: ReadonlyArray<{ id: Sort; label: string }> = [
  { id: "activity", label: "recent activity" },
  { id: "opened", label: "last opened" },
  { id: "created", label: "newest" },
  { id: "name", label: "name" },
];
const SORTERS: Record<Sort, (a: SessionInfo, b: SessionInfo) => number> = {
  activity: (a, b) => b.activity - a.activity || b.created - a.created,
  opened: (a, b) => (b.opened ?? 0) - (a.opened ?? 0) || b.activity - a.activity,
  created: (a, b) => b.created - a.created || a.name.localeCompare(b.name),
  name: (a, b) => a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: "base" }),
};
const STATE_LABEL: Record<State, string> = {
  working: "Claude is working",
  open: "open in a terminal",
  idle: "idle",
};

const VIEW_KEY = "ccpipe.picker.view";
const SORT_KEY = "ccpipe.picker.sort";
const REFRESH_MS = 10_000;
const PREVIEW_MS = 6_000;
const PREVIEW_LINES = 14;
const HEALTH_POLL_MS = 5000;
// Cap on a single in-flight probe — long enough to be patient with a
// sluggish backend, short enough to flip the dot red within one tick.
const HEALTH_TIMEOUT_MS = 4000;

// ─── small helpers ────────────────────────────────────────────────────────

function h<K extends keyof HTMLElementTagNameMap>(
  tag: K, cls?: string, text?: string,
): HTMLElementTagNameMap[K] {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  if (text !== undefined) el.textContent = text;
  return el;
}

function iconButton(svg: string, label: string, cls: string): HTMLButtonElement {
  const b = h("button", cls);
  b.type = "button";
  b.title = label;
  b.setAttribute("aria-label", label);
  b.append(iconNode(svg));
  return b;
}

function stored<T extends string>(key: string, allowed: readonly T[], fallback: T): T {
  try {
    const v = localStorage.getItem(key);
    if (v && (allowed as readonly string[]).includes(v)) return v as T;
  } catch { /* private mode */ }
  return fallback;
}
function store(key: string, value: string): void {
  try { localStorage.setItem(key, value); } catch { /* private mode */ }
}

const TIME_FMT = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" });
const DAY_FMT = new Intl.DateTimeFormat(undefined, { weekday: "short", day: "numeric", month: "short" });
const DAY_YEAR_FMT = new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short", year: "numeric" });
const FULL_FMT = new Intl.DateTimeFormat(undefined, { dateStyle: "full", timeStyle: "short" });
const startOfDay = (d: Date): number => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();

/** "today 14:05", "yesterday 09:12", "Sun 1 Nov 21:44", "3 Mar 2025 10:00". */
function dateTime(unix: number): string {
  const d = new Date(unix * 1000);
  const now = new Date();
  const days = Math.round((startOfDay(now) - startOfDay(d)) / 86_400_000);
  const time = TIME_FMT.format(d);
  if (days === 0) return `today ${time}`;
  if (days === 1) return `yesterday ${time}`;
  if (d.getFullYear() === now.getFullYear()) return `${DAY_FMT.format(d)} ${time}`;
  return `${DAY_YEAR_FMT.format(d)} ${time}`;
}
const fullDate = (unix: number): string => FULL_FMT.format(new Date(unix * 1000));

function relativeTime(unix: number): string {
  const diff = Math.max(0, Math.floor(Date.now() / 1000) - unix);
  if (diff < 45) return "just now";
  if (diff < 3600) return `${Math.max(1, Math.round(diff / 60))}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  if (diff < 86400 * 30) return `${Math.floor(diff / 86400)}d ago`;
  return dateTime(unix);
}

function basename(path: string): string {
  const trimmed = path.replace(/\/+$/, "");
  const idx = trimmed.lastIndexOf("/");
  return idx >= 0 ? trimmed.slice(idx + 1) : trimmed;
}

/** /home/<user>/x → ~/x. We don't know $HOME here; the /home/<user> prefix
 * is a safe heuristic for shortening what's only displayed. */
function homeRelative(path: string): string {
  const m = path.match(/^\/home\/[^/]+(\/.*)?$/);
  return m ? "~" + (m[1] ?? "") : path;
}

/** The task Claude Code put in the pane title, and whether it's working
 * (a braille spinner in front rather than ✳). */
function taskOf(s: SessionInfo): { text: string | null; working: boolean } {
  const raw = (s.title ?? "").trim();
  if (!raw) return { text: null, working: false };
  const first = raw.codePointAt(0) ?? 0;
  const working = first >= 0x2800 && first <= 0x28ff;
  const text = raw.replace(/^[^\p{L}\p{N}]\s+/u, "").trim();
  if (!text || text === "Claude Code" || text === s.name) return { text: null, working };
  return { text, working };
}

function stateOf(s: SessionInfo): State {
  if (taskOf(s).working) return "working";
  return (s.clients ?? (s.attached ? 1 : 0)) > 0 ? "open" : "idle";
}

function matches(s: SessionInfo, query: string): boolean {
  if (!query) return true;
  const hay = `${s.name}\n${s.title ?? ""}\n${s.cwd ?? ""}`.toLowerCase();
  return query.toLowerCase().split(/\s+/).filter(Boolean).every((w) => hay.includes(w));
}

/** The screen minus Claude Code's prompt box and footer at the bottom
 * (a rule, the ❯ line, a rule, status lines), last PREVIEW_LINES lines. */
function previewText(lines: string[]): string {
  let end = lines.length;
  for (let i = lines.length - 1; i > 0; i--) {
    const promptLine = /^\s*[❯>](\s|$)/.test(lines[i]) && /^\s*[─━]{8,}\s*$/.test(lines[i - 1]);
    const boxedPrompt = /^\s*│\s*>/.test(lines[i]) && /^\s*╭─/.test(lines[i - 1]);
    if (promptLine || boxedPrompt) { end = i - 1; break; }
  }
  const out = lines.slice(0, end);
  while (out.length && !out[out.length - 1].trim()) out.pop();
  return out.slice(-PREVIEW_LINES).join("\n");
}

function defaultView(): View {
  try { return matchMedia("(min-width: 900px)").matches ? "grid" : "detailed"; } catch { return "detailed"; }
}

// Teardown of the previous render's timers and document listeners, so going
// picker → terminal → picker never stacks them (each would pin the old DOM).
let teardownPrevious: (() => void) | null = null;

export function renderSessionPicker(
  root: HTMLElement,
  onPick: (session: string) => void,
): void {
  teardownPrevious?.();
  root.replaceChildren();

  let view: View = stored(VIEW_KEY, VIEWS.map((v) => v.id), defaultView());
  let sort: Sort = stored(SORT_KEY, SORTS.map((s) => s.id), "activity");
  let query = "";
  let sessions: SessionInfo[] = [];
  let order: string[] = [];
  let loaded = false;
  let firstPaint = true;
  let lastPointer = 0;
  const previews = new Map<string, string[]>();
  const previewTargets = new Map<string, HTMLElement>();
  const visible = new Set<string>();
  // In-flight kill-confirm timers, cancelled when the list re-renders so a
  // removed row's closure isn't pinned for the 5 s confirm window.
  const killTimers = new Set<number>();
  const cleanups: Array<() => void> = [];
  const listen = <K extends keyof DocumentEventMap>(
    type: K, fn: (e: DocumentEventMap[K]) => void, opts?: AddEventListenerOptions,
  ): void => {
    document.addEventListener(type, fn, opts);
    cleanups.push(() => document.removeEventListener(type, fn, opts));
  };

  // ─── Frame + hero ───────────────────────────────────────────────────────
  const frame = h("div", "frame frame--picker");
  const inner = h("div", "frame__inner picker-shell");

  const hero = h("header", "hero");
  const heroText = h("div", "hero__text");
  const word = h("div", "wordmark large hero__word");
  const wordDot = h("span", "dot");
  word.append("cc", wordDot, "pipe");
  const status = h("div", "hero__status");
  const statusDot = h("span", "hero__dot");
  const statusLabel = h("span", "hero__state", "checking…");
  const statusLatency = h("span", "hero__latency");
  statusLatency.hidden = true;
  const statusCount = h("span", "hero__count");
  status.append(statusDot, statusLabel, statusLatency, statusCount);
  heroText.append(word, status);
  const gear = iconButton(GEAR_SVG, "Settings", "hero__gear");
  gear.addEventListener("click", async () => {
    const { openSettings } = await import("./settings");
    openSettings({
      authRequired: true,
      onDisplayPrefsChange: () => {},
      onSessionInvalidated: () => {
        clearLastSession();
        root.replaceChildren();
        location.reload();
      },
    });
  });
  hero.append(sparkMark("spark--hero"), heroText, gear);

  // ─── Toolbar: filter · sort · view · new ────────────────────────────────
  const toolbar = h("div", "picker__toolbar");
  toolbar.setAttribute("role", "toolbar");
  toolbar.setAttribute("aria-label", "Sessions");

  const search = h("label", "picker__search");
  const searchInput = h("input", "picker__search__input");
  searchInput.type = "search";
  searchInput.placeholder = "filter sessions";
  searchInput.setAttribute("aria-label", "Filter sessions by name, task or directory");
  searchInput.spellcheck = false;
  searchInput.autocapitalize = "none";
  searchInput.autocomplete = "off";
  const searchKey = h("kbd", "picker__search__key", "/");
  searchKey.title = "Press / to filter";
  search.append(iconNode(ICON_SEARCH), searchInput, searchKey);

  const sortWrap = h("label", "picker__sort");
  sortWrap.append(h("span", "picker__sort__label", "sort"));
  const sortSel = h("select", "picker__sort__select");
  sortSel.setAttribute("aria-label", "Sort sessions by");
  for (const o of SORTS) {
    const opt = h("option", undefined, o.label);
    opt.value = o.id;
    sortSel.append(opt);
  }
  sortSel.value = sort;
  sortWrap.append(sortSel);

  const seg = h("div", "seg");
  seg.setAttribute("role", "radiogroup");
  seg.setAttribute("aria-label", "View");
  const segButtons = VIEWS.map((v) => {
    const b = iconButton(v.icon, v.label, "seg__btn");
    b.setAttribute("role", "radio");
    b.dataset.view = v.id;
    b.addEventListener("click", () => setView(v.id));
    seg.append(b);
    return b;
  });

  const newBtn = h("button", "btn btn--primary picker__new");
  newBtn.type = "button";
  newBtn.setAttribute("aria-expanded", "false");
  newBtn.append(iconNode(ICON_PLUS), h("span", undefined, "new"));
  newBtn.title = "Start a new session";
  toolbar.append(search, sortWrap, seg, newBtn);

  // ─── New-session panel (opened from the toolbar) ────────────────────────
  // grid-template-rows 0fr ↔ 1fr animates the height without measuring.
  const create = h("section", "picker__create");
  create.dataset.expanded = "false";
  create.setAttribute("aria-label", "New session");
  const createBodyWrap = h("div", "picker__create__body-wrap");
  const createBody = h("div", "picker__create__body");
  createBodyWrap.append(createBody);
  create.append(createBodyWrap);

  const createHead = h("div", "picker__create__head");
  const createClose = iconButton(ICON_X, "Close", "picker__create__close");
  createHead.append(h("span", "picker__create__title", "new session"), createClose);
  createBody.append(createHead);

  // Working directory
  const cwdField = h("div", "picker__field");
  const cwdLabel = h("label", undefined, "working directory");
  const cwdRow = h("div", "picker__cwd-row");
  const cwdInput = h("input");
  cwdInput.type = "text";
  cwdInput.placeholder = "/home/you/projects/foo";
  cwdInput.spellcheck = false;
  cwdInput.autocapitalize = "none";
  cwdInput.autocomplete = "off";
  cwdInput.id = "picker-cwd";
  cwdLabel.htmlFor = cwdInput.id;
  const browseBtn = h("button", "btn btn--ghost");
  browseBtn.type = "button";
  browseBtn.append(iconNode(FOLDER_SVG), h("span", undefined, "browse…"));
  cwdRow.append(cwdInput, browseBtn);
  const recentBox = h("div", "picker__recent");
  cwdField.append(cwdLabel, cwdRow, recentBox);
  createBody.append(cwdField);

  // Resume list (hidden until a cwd resolves)
  const resumeBox = h("div", "picker__resume");
  resumeBox.hidden = true;
  createBody.append(resumeBox);

  // Session name
  const nameField = h("div", "picker__field");
  const nameLabel = h("label", undefined, "session name");
  const nameInput = h("input");
  nameInput.placeholder = "auto-filled from directory";
  nameInput.spellcheck = false;
  nameInput.autocapitalize = "none";
  nameInput.autocomplete = "off";
  nameInput.id = "picker-name";
  nameLabel.htmlFor = nameInput.id;
  nameField.append(nameLabel, nameInput);
  createBody.append(nameField);

  const actionRow = h("div", "picker__create__actions");
  const startBtn = h("button", "btn btn--primary", "start session");
  startBtn.type = "button";
  actionRow.append(startBtn);
  createBody.append(actionRow);

  const errBox = h("div", "error");
  errBox.setAttribute("role", "alert");
  const thead = h("div", "picker__thead");
  thead.setAttribute("aria-hidden", "true");
  const list = h("div", "picker__list");

  inner.append(hero, toolbar, create, errBox, thead, list);
  frame.append(inner);
  root.append(frame);

  const showError = (msg: string): void => { errBox.textContent = msg; };

  const openCreate = (focus: boolean): void => {
    create.dataset.expanded = "true";
    newBtn.setAttribute("aria-expanded", "true");
    if (focus) setTimeout(() => cwdInput.focus(), 220);
    setTimeout(() => create.scrollIntoView({ block: "nearest", behavior: "smooth" }), 60);
  };
  const closeCreate = (): void => {
    create.dataset.expanded = "false";
    newBtn.setAttribute("aria-expanded", "false");
  };
  newBtn.addEventListener("click", () => {
    if (create.dataset.expanded === "true") closeCreate(); else openCreate(true);
  });
  createClose.addEventListener("click", closeCreate);

  // ─── Backend reachability (hero status line) ────────────────────────────
  // Polls /api/health: green < 100 ms, amber < 300 ms, red on failure. The
  // first probe waits for idle so page-boot work doesn't inflate the RTT
  // (see [[feedback-rtt-measurement-pitfalls]]).
  const setStatus = (cls: "ok" | "warn" | "err" | "", label: string, latency: string): void => {
    statusDot.className = "hero__dot" + (cls ? " " + cls : "");
    statusLabel.textContent = label;
    statusLatency.textContent = latency;
    statusLatency.hidden = !latency;
  };
  let activeProbe: AbortController | null = null;
  const probeHealth = async (): Promise<void> => {
    activeProbe?.abort();
    const ac = new AbortController();
    activeProbe = ac;
    const timeout = window.setTimeout(() => ac.abort(), HEALTH_TIMEOUT_MS);
    const t0 = performance.now();
    try {
      const res = await fetch("/api/health", { credentials: "same-origin", signal: ac.signal });
      const rtt = Math.round(performance.now() - t0);
      if (!res.ok) { setStatus("err", `backend ${res.status}`, ""); return; }
      setStatus(rtt < 100 ? "ok" : rtt < 300 ? "warn" : "err", "online", `${rtt} ms`);
    } catch (err) {
      // An abort is the timeout or a newer probe; the next tick tells.
      if ((err as Error)?.name === "AbortError") return;
      setStatus("err", "offline", "");
    } finally {
      clearTimeout(timeout);
      if (activeProbe === ac) activeProbe = null;
    }
  };
  let idleHandle: number | null = null;
  let firstProbeTimer: number | null = null;
  if (typeof window.requestIdleCallback === "function") {
    idleHandle = window.requestIdleCallback(() => { void probeHealth(); }, { timeout: 500 });
  } else {
    firstProbeTimer = window.setTimeout(() => { void probeHealth(); }, 0);
  }
  const healthTimer = window.setInterval(() => {
    if (!frame.isConnected) { teardown(); return; }
    if (!document.hidden) void probeHealth();
  }, HEALTH_POLL_MS);
  cleanups.push(() => {
    clearInterval(healthTimer);
    if (firstProbeTimer !== null) clearTimeout(firstProbeTimer);
    if (idleHandle !== null && typeof window.cancelIdleCallback === "function") {
      window.cancelIdleCallback(idleHandle);
    }
    activeProbe?.abort();
  });

  // ─── Recent dirs ────────────────────────────────────────────────────────
  const renderRecent = (): void => {
    recentBox.replaceChildren();
    const dirs = loadRecentDirs();
    recentBox.hidden = dirs.length === 0;
    if (!dirs.length) return;
    recentBox.append(h("span", "picker__recent__label", "recent:"));
    for (const d of dirs) {
      const chip = h("button", "chip", homeRelative(d));
      chip.type = "button";
      chip.title = d;
      chip.addEventListener("click", () => {
        cwdInput.value = d;
        void onCwdChanged();
      });
      recentBox.append(chip);
    }
  };

  // ─── Resume list for the chosen directory ───────────────────────────────
  // No row selected → Start means "start fresh"; a selected row → "resume".
  let resumeSelectedId: string | null = null;
  const renderResumeList = (found: ClaudeSession[]): void => {
    resumeBox.replaceChildren();
    resumeSelectedId = null;
    startBtn.textContent = "start session";
    resumeBox.hidden = found.length === 0;
    if (!found.length) return;
    resumeBox.append(
      h("div", "picker__resume__label", `resume a claude conversation from this directory (${found.length})`),
      h("div", "picker__resume__hint", "tap one to resume it, or leave none selected to start fresh"),
    );
    for (const c of found) {
      const wrap = h("div", "resume-row-wrap");
      const row = h("button", "resume-row");
      row.type = "button";
      row.dataset.id = c.id;
      row.append(
        h("div", "resume-row__preview", c.firstUserMessage || "(no user prompt found)"),
        h("div", "resume-row__meta", `${relativeTime(c.mtime)} • ${dateTime(c.mtime)} • ${c.id.slice(0, 8)}`),
      );
      row.addEventListener("click", () => {
        const wasSelected = resumeSelectedId === c.id;
        resumeBox.querySelectorAll(".resume-row.selected").forEach((r) => r.classList.remove("selected"));
        resumeSelectedId = wasSelected ? null : c.id;
        if (!wasSelected) row.classList.add("selected");
        startBtn.textContent = wasSelected ? "start session" : "resume session";
      });
      // Download the conversation as Markdown (same-origin GET, cookie auth).
      const dl = h("a", "resume-row__export");
      dl.title = "Export conversation as markdown";
      dl.append(iconNode(ICON_DOWNLOAD));
      dl.href = `/api/claude-sessions/${encodeURIComponent(c.id)}/export?cwd=${encodeURIComponent(cwdInput.value.trim())}`;
      dl.setAttribute("download", `ccpipe-${c.id.slice(0, 8)}.md`);
      dl.addEventListener("click", (e) => e.stopPropagation());
      wrap.append(row, dl);
      resumeBox.append(wrap);
    }
  };

  // cwd changed: fetch resumable conversations and auto-fill the name until
  // the user types one.
  let nameTouched = false;
  nameInput.addEventListener("input", () => { nameTouched = true; });
  const onCwdChanged = async (): Promise<void> => {
    const cwd = cwdInput.value.trim();
    if (!cwd) { resumeBox.hidden = true; return; }
    if (!nameTouched) {
      // Session names are [A-Za-z0-9_-] only (backend tmux.safe_name), so
      // fold anything else — "jamez.me.uk" → "jamez-me-uk".
      nameInput.value = basename(cwd).replace(/[^A-Za-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "") || "session";
    }
    showError("");
    try {
      const data = await apiJson<{ sessions: ClaudeSession[] }>(
        `/api/claude-sessions?cwd=${encodeURIComponent(cwd)}`,
      );
      renderResumeList(data.sessions);
    } catch {
      // The path may be half-typed or unreachable — just hide the list.
      resumeBox.hidden = true;
    }
  };
  cwdInput.addEventListener("change", () => { void onCwdChanged(); });
  cwdInput.addEventListener("blur", () => { void onCwdChanged(); });

  browseBtn.addEventListener("click", async () => {
    // Open the browser inside the fs jail root when nothing is typed yet.
    let start = cwdInput.value.trim();
    if (!start) {
      try { start = (await getFsConfig()).root; } catch { start = "/"; }
    }
    openDirectoryBrowser(document.body, {
      initialPath: start,
      onPick: (p) => { cwdInput.value = p; void onCwdChanged(); },
    });
  });

  const startSession = async (): Promise<void> => {
    const name = nameInput.value.trim();
    const cwd = cwdInput.value.trim();
    if (!name) { showError("name required"); return; }
    if (!cwd) { showError("working directory required"); return; }
    if (!cwd.startsWith("/")) { showError("cwd must be an absolute path"); return; }
    showError("");
    startBtn.disabled = true;
    try {
      const body: Record<string, string> = { name, cwd };
      if (resumeSelectedId) body.resumeSessionId = resumeSelectedId;
      await apiJson<SessionInfo>("/api/sessions", { method: "POST", body: JSON.stringify(body) });
      pushRecentDir(cwd);
      onPick(name);
    } catch (err) {
      showError(`failed to create: ${(err as Error).message}`);
    } finally {
      startBtn.disabled = false;
    }
  };
  startBtn.addEventListener("click", () => { void startSession(); });
  nameInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); void startSession(); }
  });

  // ─── Previews (grid) ────────────────────────────────────────────────────
  const paintPreview = (el: HTMLElement, lines: string[] | undefined): void => {
    el.classList.toggle("is-loading", lines === undefined);
    el.textContent = lines === undefined ? "" : (previewText(lines) || "(empty screen)");
  };
  let previewSoon: number | null = null;
  const fetchPreviews = async (): Promise<void> => {
    if (view !== "grid" || document.hidden || !frame.isConnected) return;
    const names = [...visible].filter((n) => previewTargets.has(n));
    if (!names.length) return;
    try {
      const res = await apiJson<{ previews: Record<string, string[]> }>(
        `/api/sessions/previews?lines=40&names=${encodeURIComponent(names.join(","))}`,
      );
      for (const [n, lines] of Object.entries(res.previews)) {
        previews.set(n, lines);
        const el = previewTargets.get(n);
        if (el) paintPreview(el, lines);
      }
    } catch { /* keep what we have; next tick retries */ }
  };
  const schedulePreviews = (ms: number): void => {
    if (previewSoon !== null) clearTimeout(previewSoon);
    previewSoon = window.setTimeout(() => { previewSoon = null; void fetchPreviews(); }, ms);
  };
  const observer = typeof IntersectionObserver === "function"
    ? new IntersectionObserver((entries) => {
      let fresh = false;
      for (const en of entries) {
        const n = (en.target as HTMLElement).dataset.name ?? "";
        if (en.isIntersecting) {
          visible.add(n);
          if (!previews.has(n)) fresh = true;
        } else {
          visible.delete(n);
        }
      }
      if (fresh) schedulePreviews(120);
    }, { rootMargin: "200px 0px" })
    : null;
  const previewTimer = window.setInterval(() => { void fetchPreviews(); }, PREVIEW_MS);
  cleanups.push(() => {
    clearInterval(previewTimer);
    if (previewSoon !== null) clearTimeout(previewSoon);
    observer?.disconnect();
  });

  // ─── Session items ──────────────────────────────────────────────────────
  const closeMenus = (except?: HTMLElement): void => {
    list.querySelectorAll<HTMLElement>(".sess__menu:not([hidden])").forEach((m) => {
      if (m !== except) m.hidden = true;
    });
  };

  const dateCell = (kind: "opened" | "created", unix: number | undefined): HTMLElement => {
    const el = h("span", `sess__date sess__date--${kind}`);
    el.append(h("span", "sess__date-k", kind), h("span", "sess__date-v", unix ? dateTime(unix) : "—"));
    if (unix) el.title = `${kind === "opened" ? "Last opened" : "Created"} ${fullDate(unix)}`;
    return el;
  };

  const buildActions = (s: SessionInfo, nameEl: HTMLElement): HTMLElement => {
    const actions = h("div", "sess__actions");
    const kebab = iconButton(KEBAB_SVG, `Actions for ${s.name}`, "sess__kebab");
    kebab.setAttribute("aria-haspopup", "menu");
    const menu = h("div", "sess__menu");
    menu.hidden = true;
    menu.setAttribute("role", "menu");
    const menuItem = (label: string, cls = ""): HTMLButtonElement => {
      const b = h("button", `sess__menu__item ${cls}`.trim(), label);
      b.type = "button";
      b.setAttribute("role", "menuitem");
      return b;
    };
    const openItem = menuItem("open");
    const renameItem = menuItem("rename");
    const stickyItem = menuItem(s.sticky ? "make ephemeral" : "make sticky");
    stickyItem.title = s.sticky
      ? "Stop recreating this session after a restart"
      : "Recreate this session (claude --continue) after a restart";
    const killItem = menuItem("kill", "danger");
    menu.append(openItem, renameItem, stickyItem, killItem);
    actions.append(kebab, menu);

    const closeMenu = (): void => { menu.hidden = true; };
    kebab.addEventListener("click", (e) => {
      e.stopPropagation();
      if (!menu.hidden) { closeMenu(); return; }
      closeMenus(menu);
      // Reveal first, then measure: a hidden menu has no height. Flip it
      // upward when it would run off the bottom of the viewport.
      menu.classList.remove("sess__menu--up");
      menu.hidden = false;
      if (window.innerHeight - kebab.getBoundingClientRect().bottom < menu.offsetHeight + 16) {
        menu.classList.add("sess__menu--up");
      }
    });
    openItem.addEventListener("click", (e) => { e.stopPropagation(); closeMenu(); onPick(s.name); });
    renameItem.addEventListener("click", (e) => {
      e.stopPropagation();
      closeMenu();
      startRename(s, nameEl);
    });
    stickyItem.addEventListener("click", async (e) => {
      e.stopPropagation();
      closeMenu();
      try {
        await apiJson<SessionInfo>(`/api/sessions/${encodeURIComponent(s.name)}/sticky`,
          { method: "POST", body: JSON.stringify({ sticky: !s.sticky }) });
        void load("full");
      } catch (err) {
        showError(`sticky toggle failed: ${(err as Error).message}`);
      }
    });
    // Kill with an inline confirm: the first tap arms it for 5 s.
    let armed: number | null = null;
    const disarm = (): void => {
      killItem.textContent = "kill";
      killItem.classList.remove("armed");
      if (armed !== null) { clearTimeout(armed); killTimers.delete(armed); armed = null; }
    };
    killItem.addEventListener("click", async (e) => {
      e.stopPropagation();
      if (armed === null) {
        killItem.textContent = `confirm kill ${s.name}`;
        killItem.classList.add("armed");
        armed = window.setTimeout(disarm, 5000);
        killTimers.add(armed);
        return;
      }
      disarm();
      closeMenu();
      try {
        await apiJson(`/api/sessions/${encodeURIComponent(s.name)}`, { method: "DELETE" });
        void load("full");
      } catch (err) {
        showError(`failed to kill ${s.name}: ${(err as Error).message}`);
      }
    });
    return actions;
  };

  // Inline rename: the name becomes an input; Enter / blur commit, Escape
  // cancels. `settled` stops Enter's commit and the blur that follows
  // (replaceWith fires blur) from both committing.
  const startRename = (s: SessionInfo, nameEl: HTMLElement): void => {
    const input = h("input", "sess__rename");
    input.type = "text";
    input.value = s.name;
    input.spellcheck = false;
    input.autocapitalize = "none";
    input.autocomplete = "off";
    input.setAttribute("aria-label", `New name for ${s.name}`);
    nameEl.replaceWith(input);
    input.focus();
    input.select();
    let settled = false;
    const restore = (): void => { input.replaceWith(nameEl); };
    const commit = async (): Promise<void> => {
      if (settled) return;
      settled = true;
      const newName = input.value.trim();
      if (!newName || newName === s.name) { restore(); return; }
      try {
        await apiJson<SessionInfo>(`/api/sessions/${encodeURIComponent(s.name)}`,
          { method: "PATCH", body: JSON.stringify({ newName }) });
        restore();
        void load("full");
      } catch (err) {
        showError(`rename failed: ${(err as Error).message}`);
        restore();
      }
    };
    input.addEventListener("keydown", (e) => {
      e.stopPropagation();
      if (e.key === "Enter") { e.preventDefault(); void commit(); }
      else if (e.key === "Escape") {
        e.preventDefault();
        if (!settled) { settled = true; restore(); }
      }
    });
    input.addEventListener("click", (e) => e.stopPropagation());
    input.addEventListener("blur", () => { void commit(); });
  };

  const renderItem = (s: SessionInfo, i: number): HTMLElement => {
    const item = h("div", `sess sess--${view}`);
    const state = stateOf(s);
    const task = taskOf(s);
    item.dataset.name = s.name;
    item.dataset.state = state;
    item.tabIndex = 0;
    item.setAttribute("role", "button");
    item.setAttribute("aria-label", `Open session ${s.name}${task.text ? ` — ${task.text}` : ""}`);
    if (firstPaint) item.style.animation = `frame-in 360ms ${Math.min(i, 14) * 22}ms var(--ease-out) backwards`;

    const dot = h("span", "sess__dot");
    dot.title = STATE_LABEL[state];

    const titleRow = h("div", "sess__title");
    const nameEl = h("span", "sess__name", s.name);
    titleRow.append(nameEl);
    if (s.sticky) {
      const pin = h("span", "sess__pin");
      pin.title = "Sticky: recreated after a restart";
      pin.append(iconNode(PIN_SVG));
      titleRow.append(pin);
    }
    const clients = s.clients ?? (s.attached ? 1 : 0);
    if (clients > 0) {
      const b = h("span", "sess__badge sess__badge--open", clients > 1 ? `open ×${clients}` : "open");
      b.title = `${clients} terminal${clients > 1 ? "s" : ""} attached right now`;
      titleRow.append(b);
    }
    if (s.windows > 1) {
      const b = h("span", "sess__badge", `${s.windows} windows`);
      titleRow.append(b);
    }

    // Never `hidden`: in the list views it holds a grid column even empty.
    const taskEl = h("div", "sess__task", task.text ?? "");
    if (task.text) taskEl.title = task.text;

    const activity = h("span", "sess__activity");
    if (state === "working") {
      activity.classList.add("is-working");
      activity.textContent = "working";
    } else {
      activity.textContent = s.activity ? relativeTime(s.activity) : "—";
    }
    if (s.activity) activity.title = `Last output ${fullDate(s.activity)}`;

    const cwd = h("span", "sess__cwd", s.cwd ? homeRelative(s.cwd) : "");
    if (s.cwd) cwd.title = s.cwd;
    const dates = h("div", "sess__dates");
    dates.append(dateCell("opened", s.opened || undefined), dateCell("created", s.created || undefined));

    const actions = buildActions(s, nameEl);

    if (view === "compact") {
      item.append(dot, titleRow, taskEl, activity, actions);
    } else if (view === "detailed") {
      const main = h("div", "sess__main");
      main.append(titleRow, taskEl);
      item.append(dot, main, cwd, activity, dates, actions);
    } else {
      const head = h("div", "sess__head");
      head.append(dot, titleRow, actions);
      const preview = h("pre", "sess__preview");
      preview.dataset.name = s.name;
      preview.setAttribute("aria-hidden", "true");
      paintPreview(preview, previews.get(s.name));
      previewTargets.set(s.name, preview);
      observer?.observe(preview);
      if (!observer) visible.add(s.name);
      const foot = h("div", "sess__foot");
      foot.append(cwd, activity);
      item.append(head, taskEl, preview, foot, dates);
    }

    item.addEventListener("click", (e) => {
      if (actions.contains(e.target as Node)) return;
      if ((e.target as HTMLElement).closest(".sess__rename")) return;
      onPick(s.name);
    });
    item.addEventListener("keydown", (e) => {
      if (e.target !== item) return;
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onPick(s.name); }
    });
    return item;
  };

  // ─── Rendering the list ─────────────────────────────────────────────────
  const syncControls = (): void => {
    inner.dataset.view = view;
    for (const b of segButtons) {
      const on = b.dataset.view === view;
      b.setAttribute("aria-checked", on ? "true" : "false");
      b.classList.toggle("is-active", on);
    }
    sortSel.value = sort;
  };

  const renderThead = (): void => {
    thead.replaceChildren(h("span"));
    const col = (label: string, key: Sort | null): HTMLElement => {
      if (!key) return h("span", "picker__th", label);
      const b = h("button", "picker__th picker__th--sort", label);
      b.type = "button";
      b.tabIndex = -1;
      b.classList.toggle("is-active", sort === key);
      b.addEventListener("click", () => setSort(key));
      return b;
    };
    thead.append(col("session", "name"), col("directory", null), col("activity", "activity"),
      col("last opened", "opened"), col("created", "created"), h("span"));
  };

  const updateCount = (): void => {
    const working = sessions.filter((s) => stateOf(s) === "working").length;
    const open = sessions.filter((s) => stateOf(s) === "open").length;
    const parts = [`${sessions.length} session${sessions.length === 1 ? "" : "s"}`];
    if (working) parts.push(`${working} working`);
    if (open) parts.push(`${open} open`);
    statusCount.textContent = loaded ? parts.join(" · ") : "";
  };

  const render = (resort: boolean): void => {
    for (const t of killTimers) clearTimeout(t);
    killTimers.clear();
    syncControls();
    if (resort || order.length === 0) {
      order = [...sessions].sort(SORTERS[sort]).map((s) => s.name);
    } else {
      // Keep the order the user is looking at; new sessions go on top.
      const idx = new Map(order.map((n, i) => [n, i]));
      order = [...sessions]
        .sort((a, b) => (idx.get(a.name) ?? -1) - (idx.get(b.name) ?? -1) || SORTERS[sort](a, b))
        .map((s) => s.name);
    }
    const byName = new Map(sessions.map((s) => [s.name, s]));
    const shown = order.map((n) => byName.get(n)!).filter((s) => matches(s, query));

    observer?.disconnect();
    previewTargets.clear();
    visible.clear();
    list.className = `picker__list picker__list--${view}`;
    thead.hidden = view !== "detailed" || shown.length === 0;
    if (!thead.hidden) renderThead();
    updateCount();
    list.replaceChildren();

    if (!loaded) return;
    if (sessions.length === 0) {
      const empty = h("div", "picker__empty");
      empty.append(
        sparkMark("spark--empty"),
        h("div", "picker__empty__title", "no sessions yet"),
        h("div", "picker__empty__hint",
          "pick a project directory below and start your first claude session. "
          + "existing conversations in that directory show up as resume options."),
      );
      list.append(empty);
      list.classList.add("picker__list--empty");
      openCreate(false);
      return;
    }
    if (!shown.length) {
      const none = h("div", "picker__nomatch", `no session matches “${query}”`);
      list.append(none);
      return;
    }
    shown.forEach((s, i) => list.append(renderItem(s, i)));
    firstPaint = false;
    if (view === "grid") schedulePreviews(60);
  };

  // A soft refresh doesn't rebuild the list while the user is in the middle
  // of something on it.
  const interacting = (): boolean =>
    Date.now() - lastPointer < 1500
    || list.contains(document.activeElement)
    || !!list.querySelector(".sess__menu:not([hidden]), .sess__rename");

  let loadSeq = 0;
  const load = async (mode: "full" | "soft"): Promise<void> => {
    const mine = ++loadSeq;
    let fresh: SessionInfo[];
    try {
      fresh = await apiJson<SessionInfo[]>("/api/sessions");
    } catch (err) {
      if (mode === "full") showError(`failed to list sessions: ${(err as Error).message}`);
      return;
    }
    if (mine !== loadSeq || !frame.isConnected) return;
    if (mode === "soft" && interacting()) return;
    if (mode === "full" && errBox.textContent?.startsWith("failed to list")) showError("");
    sessions = fresh;
    loaded = true;
    render(mode === "full");
  };

  const setView = (next: View): void => {
    if (next === view) return;
    view = next;
    store(VIEW_KEY, view);
    firstPaint = true;
    render(false);
  };
  const setSort = (next: Sort): void => {
    sort = next;
    store(SORT_KEY, sort);
    render(true);
  };
  sortSel.addEventListener("change", () => setSort(sortSel.value as Sort));

  searchInput.addEventListener("input", () => {
    query = searchInput.value.trim();
    render(false);
  });
  searchInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      // Enter opens the first match.
      const first = list.querySelector<HTMLElement>(".sess");
      if (first?.dataset.name) { e.preventDefault(); onPick(first.dataset.name); }
    } else if (e.key === "Escape") {
      searchInput.value = "";
      query = "";
      render(false);
      searchInput.blur();
    }
  });

  // ─── Document-level wiring (removed on teardown) ────────────────────────
  list.addEventListener("pointerdown", () => { lastPointer = Date.now(); });
  listen("click", (e) => {
    const t = e.target as Element | null;
    if (!t?.closest?.(".sess__actions")) closeMenus();
  });
  listen("keydown", (e) => {
    if (e.key === "Escape") { closeMenus(); return; }
    if (e.key !== "/" || e.ctrlKey || e.metaKey || e.altKey) return;
    const a = document.activeElement as HTMLElement | null;
    if (a && (a.tagName === "INPUT" || a.tagName === "TEXTAREA" || a.tagName === "SELECT" || a.isContentEditable)) return;
    if (document.querySelector(".modal-overlay")) return;
    e.preventDefault();
    searchInput.focus();
  });
  listen("visibilitychange", () => {
    if (!document.hidden) { void probeHealth(); void load("full"); }
  });
  const refreshTimer = window.setInterval(() => {
    if (!document.hidden) void load("soft");
  }, REFRESH_MS);
  cleanups.push(() => clearInterval(refreshTimer));

  function teardown(): void {
    for (const fn of cleanups.splice(0)) {
      try { fn(); } catch { /* keep tearing down */ }
    }
    for (const t of killTimers) clearTimeout(t);
    killTimers.clear();
    if (teardownPrevious === teardown) teardownPrevious = null;
  }
  teardownPrevious = teardown;
  frame.addEventListener("ccpipe:dispose", teardown, { once: true });

  // ─── First render ───────────────────────────────────────────────────────
  syncControls();
  renderRecent();
  void load("full");
  // Default the directory to the most recent one, else the fs jail root.
  const dirs = loadRecentDirs();
  if (dirs[0]) {
    cwdInput.value = dirs[0];
    void onCwdChanged();
  } else {
    void getFsConfig().then((cfg) => {
      cwdInput.value = cfg.root;
      void onCwdChanged();
    }).catch(() => {
      cwdInput.value = "/";
      void onCwdChanged();
    });
  }
}
