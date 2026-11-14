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
// "Working" is Claude Code's own status (its session file; the pane
// title's spinner on older versions). A session that needs you, Claude's
// question or a finished turn no one has looked at (backend attention.py),
// is marked and goes to the top when the list is sorted. When one starts, a
// banner names it, its row flashes, and it chimes and its name is said
// (Settings → Notifications).

import { apiJson, getFsConfig } from "./api";
import { sparkMark } from "./brand";
import { h } from "./dom";
import { addBusyCheck } from "./update";
import { openDirectoryBrowser } from "./directory-browser";
import { clearLastSession, takePickerNotice } from "./display-prefs";
import {
  FOLDER_SVG, GEAR_SVG, ICON_DOWNLOAD, ICON_PLUS, ICON_SEARCH, ICON_VIEW_COMPACT,
  ICON_VIEW_DETAILED, ICON_VIEW_GRID, ICON_X, KEBAB_SVG, PIN_SVG, iconNode,
} from "./icons";
import { loadRecentDirs, pushRecentDir } from "./recent-dirs";
import { FAST_PING_MS, LatencyLog, latencyLevel, pingDropdown } from "./pingmeter";
import { mountSysPanel } from "./syspanel";
import {
  NewItems, ago, canSound, chime, claimAlert, clearToasts, deviceId, installChimeUnlock,
  loadAlerts, sayName, setAppBadge, setTitleCount, toast, whatLabel, type AttentionItem,
} from "./attention";
import { fireResponseReady } from "./notifications";
import { pushCovers, tidyNotifications } from "./push";
import { bar, fmtBytes, loadStatsPrefs, onStatsPrefs } from "./sysviz";
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
  /** CPU (% of one core, like top: 200 = two cores busy), memory and
   * project disk use (bytes). */
  cpu?: number | null;
  mem?: number | null;
  disk?: number | null;
  /** Idle tracking: why it counts as in use; when it'll be stopped if idle. */
  busy?: string | null;
  stops_at?: number | null;
  /** Claude Code's own status: "busy" | "shell" (background shell task) |
   * "mcp" (background MCP task, from its footer) | "idle" | "waiting" (a
   * question or permission prompt is up). */
  claude?: string | null;
  /** What needs you here: Claude's question, or a finished turn. */
  attention?: AttentionItem | null;
  /** "background": it yields CPU and memory to the other sessions.
   * null / absent: this host can't do that, so the menu doesn't offer it. */
  priority?: "normal" | "background" | null;
  /** A background session whose memory is being held back right now. */
  mem_held?: boolean;
  /** Its Claude is older than the installed Claude Code; `blocked` is why
   * it can't be restarted on the new one right now (null = it can). */
  update?: { from: string; to: string; blocked: string | null } | null;
};

/** A remembered session that isn't running; opening it resumes it. */
type StoppedInfo = {
  name: string;
  sticky: boolean;
  created: number;
  opened: number;
  stopped: number;
  stop_reason: "idle" | "stopped" | "gone" | "update";
  cwd: string;
  conversation: boolean;
  /** Never ran Claude: it resumes as a plain shell. */
  shell?: boolean;
  disk: number | null;
};

type ClaudeSession = {
  id: string;
  mtime: number;
  size: number;
  firstUserMessage: string | null;
};

type View = "compact" | "detailed" | "grid";
type Sort = "activity" | "opened" | "created" | "name" | "cpu" | "memory" | "disk";
type State = "asking" | "working" | "task" | "done" | "open" | "idle" | "stopped";

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
  { id: "cpu", label: "cpu" },
  { id: "memory", label: "memory" },
  { id: "disk", label: "disk" },
];
const SORTERS: Record<Sort, (a: SessionInfo, b: SessionInfo) => number> = {
  activity: (a, b) => b.activity - a.activity || b.created - a.created,
  opened: (a, b) => (b.opened ?? 0) - (a.opened ?? 0) || b.activity - a.activity,
  created: (a, b) => b.created - a.created || a.name.localeCompare(b.name),
  name: (a, b) => a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: "base" }),
  cpu: (a, b) => (b.cpu ?? -1) - (a.cpu ?? -1) || a.name.localeCompare(b.name),
  memory: (a, b) => (b.mem ?? -1) - (a.mem ?? -1) || a.name.localeCompare(b.name),
  disk: (a, b) => (b.disk ?? -1) - (a.disk ?? -1) || a.name.localeCompare(b.name),
};
const STATE_LABEL: Record<State, string> = {
  asking: "Claude is waiting for you",
  working: "Claude is working",
  done: "Claude finished, and no one has looked yet",
  task: "a background task is running",
  open: "open in a terminal",
  idle: "idle",
  stopped: "stopped — open it to resume",
};

const VIEW_KEY = "ccpipe.picker.view";
const SORT_KEY = "ccpipe.picker.sort";
const REFRESH_MS = 10_000;
const PREVIEW_MS = 6_000;
const PREVIEW_LINES = 14;
const HEALTH_POLL_MS = 5000;
// How often what needs you is checked while the list is on screen. Cheap:
// the server keeps the list (attention.py).
const ATTENTION_POLL_MS = 3000;
/** Sessions that need you first when the list is sorted: asking, then
 * finished. */
const NEEDS_RANK: Partial<Record<State, number>> = { asking: 2, done: 1 };
const needsYouFirst = (a: SessionInfo, b: SessionInfo): number =>
  (NEEDS_RANK[stateOf(b)] ?? 0) - (NEEDS_RANK[stateOf(a)] ?? 0);
// Cap on a single in-flight probe — long enough to be patient with a
// sluggish backend, short enough to flip the dot red within one tick.
const HEALTH_TIMEOUT_MS = 4000;

// ─── small helpers ────────────────────────────────────────────────────────


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

/** "just now", "5m ago", "3h ago", "2d ago" (attention.ts), then a date. */
function relativeTime(unix: number): string {
  return Date.now() / 1000 - unix < 86400 * 30 ? ago(unix) : dateTime(unix);
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

/** Claude Code's own status wins (the pane title shows ✳ even mid-turn);
 * the title spinner is the fallback for older Claude Code versions. */
function stateOf(s: SessionInfo): State {
  if (s.attention?.level === "ask" || s.claude === "waiting") return "asking";
  if (s.claude === "busy" || taskOf(s).working) return "working";
  if (s.attention?.level === "done") return "done";
  if (s.claude === "shell" || s.claude === "mcp") return "task";
  return (s.clients ?? (s.attached ? 1 : 0)) > 0 ? "open" : "idle";
}

/** "45m", "3h 20m", "2d 4h". */
function fmtIn(seconds: number): string {
  const m = Math.max(1, Math.round(seconds / 60));
  if (m < 60) return `${m}m`;
  const hrs = Math.floor(m / 60);
  if (hrs < 48) return `${hrs}h${m % 60 ? ` ${m % 60}m` : ""}`;
  return `${Math.floor(hrs / 24)}d${hrs % 24 ? ` ${hrs % 24}h` : ""}`;
}

const STOP_REASON: Record<StoppedInfo["stop_reason"], string> = {
  idle: "stopped when idle",
  stopped: "stopped",
  gone: "not running",
  update: "stopped for an update",
};

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
  let stopped: StoppedInfo[] = [];
  let statsPrefs = loadStatsPrefs();
  let order: string[] = [];
  let loaded = false;
  let firstPaint = true;
  let lastPointer = 0;
  let lastKey = 0;
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
  // "1 needs you", ahead of the count: what to do first.
  const statusNeeds = h("span", "hero__needs");
  statusNeeds.hidden = true;
  // On a phone it starts a line of its own.
  const needsBreak = h("span", "hero__break");
  needsBreak.hidden = true;
  const clearDone = h("button", "hero__clear", "clear");
  clearDone.type = "button";
  clearDone.title = "Clear the finished turns";
  clearDone.hidden = true;
  // The dot, state and round trip are one button: it drops down the
  // round-trip graph and the server's ups and downs (pingmeter.ts).
  const statusLink = h("button", "hero__link");
  statusLink.type = "button";
  statusLink.title = "Connection: round trips and outages";
  statusLink.append(statusDot, statusLabel, statusLatency);
  status.append(statusLink, statusCount, needsBreak, statusNeeds, clearDone);
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

  // CPU / memory / disk with history (syspanel.ts); can be hidden in Settings.
  const sysPanel = mountSysPanel();
  cleanups.push(() => sysPanel.dispose());
  cleanups.push(onStatsPrefs((p) => { statsPrefs = p; render(false); }));

  // Why the terminal we came from closed (main.ts onSessionGone), shown
  // once. Its own line: the error box is cleared by the create panel.
  const noticeBox = h("div", "picker__notice");
  noticeBox.setAttribute("role", "status");
  const notice = takePickerNotice();
  noticeBox.hidden = !notice;
  if (notice) {
    const close = h("button", "picker__notice__close", "✕");
    close.type = "button";
    close.setAttribute("aria-label", "Dismiss");
    close.addEventListener("click", () => { noticeBox.hidden = true; });
    noticeBox.append(h("span", "picker__notice__text", notice), close);
  }

  inner.append(hero, sysPanel.el, toolbar, noticeBox, create, errBox, thead, list);
  frame.append(inner);
  root.append(frame);

  const showError = (msg: string): void => { errBox.textContent = msg; };

  // Typed into since it opened: a self-update reload would lose that, so it
  // holds the reload off (update.ts). Merely open — it opens by itself when
  // there are no sessions, prefilled — doesn't.
  let createEdited = false;
  create.addEventListener("input", () => { createEdited = true; });
  cleanups.push(addBusyCheck(() => create.dataset.expanded === "true" && createEdited));
  const openCreate = (focus: boolean): void => {
    create.dataset.expanded = "true";
    newBtn.setAttribute("aria-expanded", "true");
    if (focus) setTimeout(() => cwdInput.focus(), 220);
    setTimeout(() => create.scrollIntoView({ block: "nearest", behavior: "smooth" }), 60);
  };
  const closeCreate = (): void => {
    createEdited = false;
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
  // (see [[feedback-rtt-measurement-pitfalls]]). The readings feed the
  // drop-down, which probes every FAST_PING_MS while it's open.
  const healthLog = new LatencyLog();
  let fastProbeTimer: number | null = null;
  const stopFastProbe = (): void => {
    if (fastProbeTimer !== null) clearInterval(fastProbeTimer);
    fastProbeTimer = null;
  };
  const healthPop = pingDropdown(statusLink, healthLog, {
    linkTitle: "the ccpipe server",
    dropsLabel: "outages",
    scheme: location.protocol === "https:" ? "https" : "http",
    what: "A request goes to ccpipe",
    everyMs: HEALTH_POLL_MS,
    // No terminal here, and a swipe that starts on it must scroll the page.
    keepFocus: false,
    onOpenChange(open) {
      stopFastProbe();
      if (!open) return;
      // Its click doesn't reach the document (and a tap's never fires), so
      // close a session's menu here rather than leave both open.
      closeMenus();
      // One probe at a time: a slow one isn't cut short by the next.
      const fast = (): void => { if (!activeProbe && !document.hidden) void probeHealth(); };
      fastProbeTimer = window.setInterval(fast, FAST_PING_MS);
      fast();
    },
  });
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
    let timedOut = false;
    const timeout = window.setTimeout(() => { timedOut = true; ac.abort(); }, HEALTH_TIMEOUT_MS);
    const t0 = performance.now();
    try {
      const res = await fetch("/api/health", { credentials: "same-origin", signal: ac.signal });
      const rtt = Math.round(performance.now() - t0);
      if (!res.ok) { setStatus("err", `backend ${res.status}`, ""); healthLog.offline(); return; }
      const lvl = latencyLevel(rtt);
      setStatus(lvl === "bad" ? "err" : lvl, "online", `${rtt} ms`);
      healthLog.online();
      healthLog.add(rtt);
    } catch (err) {
      // No answer in HEALTH_TIMEOUT_MS: a hung server, or a network that
      // drops everything without saying so. Down, like a failed request.
      // Any other abort is a newer probe (or the picker closing) taking over.
      if (timedOut) { setStatus("err", "no answer", ""); healthLog.offline(); return; }
      if ((err as Error)?.name === "AbortError") return;
      setStatus("err", "offline", "");
      healthLog.offline();
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
    if (!document.hidden && fastProbeTimer === null) void probeHealth();
  }, HEALTH_POLL_MS);
  cleanups.push(() => {
    healthPop.dispose();
    stopFastProbe();
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
    inner.querySelectorAll<HTMLElement>(".sess__menu:not([hidden])").forEach((m) => {
      if (m !== except) m.hidden = true;
    });
  };

  const dateCell = (kind: "opened" | "created", unix: number | undefined): HTMLElement => {
    const el = h("span", `sess__date sess__date--${kind}`);
    el.append(h("span", "sess__date-k", kind), h("span", "sess__date-v", unix ? dateTime(unix) : "—"));
    if (unix) el.title = `${kind === "opened" ? "Last opened" : "Created"} ${fullDate(unix)}`;
    return el;
  };

  const buildActions = (s: SessionInfo, nameEl: HTMLElement, stopped?: StoppedInfo): HTMLElement => {
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
    const closeMenu = (): void => { menu.hidden = true; };
    // A destructive item asks again: the first tap arms it for 5 s.
    const confirmItem = (label: string, title: string, run: () => Promise<unknown>): HTMLButtonElement => {
      const b = menuItem(label, "danger");
      b.title = title;
      let armed: number | null = null;
      const disarm = (): void => {
        b.textContent = label;
        b.classList.remove("armed");
        if (armed !== null) { clearTimeout(armed); killTimers.delete(armed); armed = null; }
      };
      b.addEventListener("click", async (e) => {
        e.stopPropagation();
        if (armed === null) {
          b.textContent = `confirm ${label} ${s.name}`;
          b.classList.add("armed");
          armed = window.setTimeout(disarm, 5000);
          killTimers.add(armed);
          return;
        }
        disarm();
        closeMenu();
        try {
          await run();
        } catch (err) {
          showError(`${label} ${s.name} failed: ${(err as Error).message}`);
        }
        void load("full");
      });
      return b;
    };
    const enc = encodeURIComponent(s.name);
    const forgetItem = confirmItem("forget",
      "Stop it if running and remove it from the list for good",
      () => apiJson(`/api/sessions/${enc}/forget`, { method: "POST" }));

    if (stopped) {
      const resumeItem = menuItem("resume");
      resumeItem.title = stopped.shell
        ? "Start its shell again in its directory"
        : stopped.conversation
          ? "Start it again in its directory, on the conversation it was on"
          : "Start it again in its directory, on that directory's latest conversation";
      resumeItem.addEventListener("click", (e) => { e.stopPropagation(); closeMenu(); void resumeAndOpen(s.name); });
      menu.append(resumeItem, forgetItem);
    } else {
      const openItem = menuItem("open");
      const renameItem = menuItem("rename");
      const stickyItem = menuItem(s.sticky ? "make ephemeral" : "make sticky");
      stickyItem.title = s.sticky
        ? "Sticky: may start at boot (Settings → Sessions) and gets the longer idle time"
        : "Make it sticky: may start at boot and gets the longer idle time before an auto-stop";
      const stopItem = confirmItem("stop",
        "Stop its processes to free memory; it stays listed and resumes (same conversation) when opened",
        () => apiJson(`/api/sessions/${enc}/stop`, { method: "POST" }));
      menu.append(openItem, renameItem, stickyItem);
      if (s.priority) {
        const toBackground = s.priority !== "background";
        const prioItem = menuItem(toBackground ? "run in background" : "run normally");
        prioItem.title = toBackground
          ? "Keep it working, but let the other sessions have the processor and the memory first"
          : "Give it its full share of the processor and memory again";
        menu.append(prioItem);
        prioItem.addEventListener("click", async (e) => {
          e.stopPropagation();
          closeMenu();
          try {
            await apiJson<SessionInfo>(`/api/sessions/${enc}/priority`,
              { method: "POST", body: JSON.stringify({ level: toBackground ? "background" : "normal" }) });
            void load("full");
          } catch (err) {
            showError(`priority change failed: ${(err as Error).message}`);
          }
        });
      }
      if (s.update) {
        const u = s.update;
        const updItem = menuItem(`restart on ${u.to}`);
        updItem.title = u.blocked
          ? `Can't restart now: ${u.blocked}`
          : `Restart Claude Code ${u.from} on ${u.to}, on the same conversation`;
        if (u.blocked) updItem.classList.add("sess__menu__item--off");
        menu.append(updItem);
        updItem.addEventListener("click", async (e) => {
          e.stopPropagation();
          closeMenu();
          try {
            await apiJson<SessionInfo>(`/api/sessions/${enc}/update`, { method: "POST" });
          } catch (err) {
            showError(`restart of ${s.name} for the update failed: ${(err as Error).message}`);
          }
          void load("full");
        });
      }
      menu.append(stopItem, forgetItem);
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
          await apiJson<SessionInfo>(`/api/sessions/${enc}/sticky`,
            { method: "POST", body: JSON.stringify({ sticky: !s.sticky }) });
          void load("full");
        } catch (err) {
          showError(`sticky toggle failed: ${(err as Error).message}`);
        }
      });
    }
    actions.append(kebab, menu);
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
    return actions;
  };

  // Resume a stopped session (same directory, same conversation), then open
  // it. Opening alone would also resume it, but this way a problem (its
  // directory is gone, the session limit) is shown here.
  const resumeAndOpen = async (name: string): Promise<void> => {
    const el = inner.querySelector<HTMLElement>(`.sess[data-name="${CSS.escape(name)}"]`);
    el?.classList.add("is-resuming");
    showError("");
    try {
      await apiJson(`/api/sessions/${encodeURIComponent(name)}/resume`, { method: "POST" });
      onPick(name);
    } catch (err) {
      el?.classList.remove("is-resuming");
      showError(`couldn't resume ${name}: ${(err as Error).message}`);
    }
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

  // RAM / disk cell: the number, and a bar relative to the largest shown.
  const useCell = (kind: "ram" | "disk", bytes: number | null | undefined, max: number): HTMLElement => {
    const el = h("span", `sess__use sess__use--${kind}`);
    if (bytes === null || bytes === undefined) {
      el.append(h("span", "sess__use-k", kind), h("span", "sess__use-v", "—"));
      return el;
    }
    el.append(h("span", "sess__use-k", kind), h("span", "sess__use-v", fmtBytes(bytes)),
      bar(max > 0 ? (bytes / max) * 100 : 0, "sv-bar--rel"));
    el.title = kind === "ram"
      ? `${fmtBytes(bytes)} of memory: Claude, its helpers and anything they started`
      : `${fmtBytes(bytes)} on disk in its project directory`;
    return el;
  };

  // CPU cell, like top: 100% is one core busy. The bar's full width is one
  // core, or the busiest shown when that's more, so 300% vs 100% still reads.
  const cpuCell = (pct: number | null | undefined, max: number): HTMLElement => {
    const el = h("span", "sess__use sess__use--cpu");
    if (pct === null || pct === undefined) {
      el.append(h("span", "sess__use-k", "cpu"), h("span", "sess__use-v", "—"));
      return el;
    }
    const v = pct >= 10 ? `${Math.round(pct)}%` : pct > 0 ? `${pct.toFixed(1)}%` : "0%";
    el.append(h("span", "sess__use-k", "cpu"), h("span", "sess__use-v", v),
      bar((pct / Math.max(100, max)) * 100, "sv-bar--rel"));
    el.title = `${v} CPU over the last ~15 s, like top (100% = one core busy): `
      + "Claude, its helpers and anything they started";
    return el;
  };

  const asInfo = (st: StoppedInfo): SessionInfo => ({
    name: st.name, windows: 1, attached: false, created: st.created, sticky: st.sticky,
    activity: st.stopped, opened: st.opened, clients: 0, cwd: st.cwd, title: null,
    disk: st.disk, mem: null, cpu: null,
  });

  let maxCpu = 0;
  let maxMem = 0;
  let maxDisk = 0;

  const renderItem = (s: SessionInfo, i: number, stopped?: StoppedInfo): HTMLElement => {
    const item = h("div", `sess sess--${view}`);
    const state: State = stopped ? "stopped" : stateOf(s);
    const task = taskOf(s);
    const now = Date.now() / 1000;
    item.dataset.name = s.name;
    item.dataset.state = state;
    item.tabIndex = 0;
    item.setAttribute("role", "button");
    item.setAttribute("aria-label", `${stopped ? "Resume" : "Open"} session ${s.name}${task.text ? ` — ${task.text}` : ""}`);
    if (firstPaint) item.style.animation = `frame-in 360ms ${Math.min(i, 14) * 22}ms var(--ease-out) backwards`;

    const dot = h("span", "sess__dot");
    dot.title = STATE_LABEL[state] + (s.busy && state !== "working" && state !== "task" ? ` — ${s.busy}` : "");

    const titleRow = h("div", "sess__title");
    const nameEl = h("span", "sess__name", s.name);
    titleRow.append(nameEl);
    if (s.sticky) {
      const pin = h("span", "sess__pin");
      pin.title = "Sticky";
      pin.append(iconNode(PIN_SVG));
      titleRow.append(pin);
    }
    const clients = s.clients ?? (s.attached ? 1 : 0);
    if (clients > 0) {
      const b = h("span", "sess__badge sess__badge--open", clients > 1 ? `open ×${clients}` : "open");
      b.title = `${clients} terminal${clients > 1 ? "s" : ""} attached right now`;
      titleRow.append(b);
    }
    if (s.windows > 1) titleRow.append(h("span", "sess__badge", `${s.windows} windows`));
    if (s.update) {
      const b = h("span", "sess__badge sess__badge--update", "update");
      b.title = `Running Claude Code ${s.update.from}; ${s.update.to} is installed. `
        + (s.update.blocked ? `Not restarting now: ${s.update.blocked}.` : "It can be restarted now (\u22ee menu).");
      titleRow.append(b);
    }
    if (s.priority === "background") {
      const b = h("span", "sess__badge sess__badge--bg", s.mem_held ? "background · held" : "background");
      b.title = s.mem_held
        ? "Background: memory is short, so it is being held back (and partly moved to swap) until the others need less"
        : "Background: the other sessions get the processor and the memory first";
      titleRow.append(b);
    }

    // Never `hidden`: in the list views it holds a grid column even empty.
    const taskText = stopped
      ? `${STOP_REASON[stopped.stop_reason]} · resumes ${
        stopped.shell ? "as a shell" : stopped.conversation ? "its conversation" : "the latest conversation"}`
      : task.text ?? "";
    const taskEl = h("div", "sess__task", taskText);
    if (taskText) taskEl.title = taskText;

    const ask = stopped ? null : s.attention ?? null;
    if (ask?.body && (state === "asking" || state === "done")) {
      taskEl.textContent = ask.body;
      taskEl.title = ask.body;
      taskEl.classList.add(`sess__task--${state}`);
    }
    const activity = h("span", "sess__activity");
    if (state === "asking") {
      activity.classList.add("is-asking");
      activity.textContent = ask ? whatLabel(ask) : "waiting for you";
      if (ask) activity.title = `${ask.title}, ${relativeTime(ask.since)}`;
    } else if (state === "done" && ask) {
      activity.classList.add("is-done");
      activity.textContent = "finished";
      activity.title = `Claude finished ${relativeTime(ask.since)}, while no one was looking. `
        + "Opening it clears this.";
    } else if (state === "working" || state === "task") {
      activity.classList.add("is-working");
      activity.textContent = state === "working" ? "working"
        : s.claude === "mcp" ? "MCP task running" : "task running";
    } else if (stopped) {
      activity.textContent = stopped.stopped ? relativeTime(stopped.stopped) : "stopped";
      if (stopped.stopped) activity.title = `Stopped ${fullDate(stopped.stopped)}`;
    } else {
      activity.textContent = s.activity ? relativeTime(s.activity) : "—";
    }
    if (!stopped && s.activity) activity.title = `Last output ${fullDate(s.activity)}`;

    // Idle auto-stop countdown (lifecycle.py decides; nothing busy may be stopped).
    let stopEl: HTMLElement | null = null;
    // Not while Claude waits for you: that keeps it running (its once-a-
    // minute check may not have seen the question yet).
    if (!stopped && s.stops_at && state !== "asking") {
      stopEl = h("div", "sess__stop", s.stops_at > now ? `auto-stop in ${fmtIn(s.stops_at - now)}` : "auto-stop due");
      stopEl.title = "Idle — no terminal attached, no output, no task running, no CPU use. "
        + "It stops then, and resumes the same conversation when opened (Settings → Sessions).";
    }

    const cwd = h("span", "sess__cwd", s.cwd ? homeRelative(s.cwd) : "");
    if (s.cwd) cwd.title = s.cwd;
    const dates = h("div", "sess__dates");
    dates.append(dateCell("opened", s.opened || undefined), dateCell("created", s.created || undefined));
    const cpu = cpuCell(stopped ? undefined : s.cpu, maxCpu);
    const ram = useCell("ram", stopped ? undefined : s.mem, maxMem);
    const disk = useCell("disk", s.disk, maxDisk);

    const actions = buildActions(s, nameEl, stopped);

    if (view === "compact") {
      item.append(dot, titleRow, taskEl, activity, actions);
    } else if (view === "detailed") {
      const main = h("div", "sess__main");
      main.append(titleRow, taskEl);
      if (stopEl) main.append(stopEl);
      // Two table columns on a wide screen (display: contents); one short
      // line on a phone.
      const uses = h("div", "sess__uses");
      uses.append(cpu, ram, disk);
      item.append(dot, main, cwd, activity, uses, dates, actions);
    } else {
      const head = h("div", "sess__head");
      head.append(dot, titleRow, actions);
      const preview = h("pre", "sess__preview");
      preview.setAttribute("aria-hidden", "true");
      if (stopped) {
        preview.classList.add("sess__preview--stopped");
        preview.textContent = "stopped · tap to resume";
      } else {
        preview.dataset.name = s.name;
        paintPreview(preview, previews.get(s.name));
        previewTargets.set(s.name, preview);
        observer?.observe(preview);
        if (!observer) visible.add(s.name);
      }
      const foot = h("div", "sess__foot");
      foot.append(cwd, activity);
      const use = h("div", "sess__uses");
      use.append(cpu, ram, disk);
      item.append(head, taskEl, preview, foot, use);
      if (stopEl) item.append(stopEl);
      item.append(dates);
    }

    const go = (): void => { if (stopped) void resumeAndOpen(s.name); else onPick(s.name); };
    item.addEventListener("click", (e) => {
      if (actions.contains(e.target as Node)) return;
      if ((e.target as HTMLElement).closest(".sess__rename")) return;
      go();
    });
    item.addEventListener("keydown", (e) => {
      if (e.target !== item) return;
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); }
    });
    return item;
  };

  // ─── Rendering the list ─────────────────────────────────────────────────
  const syncControls = (): void => {
    inner.dataset.view = view;
    inner.dataset.use = statsPrefs.perSession ? "on" : "off";
    for (const b of segButtons) {
      const on = b.dataset.view === view;
      b.setAttribute("aria-checked", on ? "true" : "false");
      b.classList.toggle("is-active", on);
    }
    sortSel.value = sort;
  };

  const renderThead = (): void => {
    thead.replaceChildren(h("span"));
    const col = (label: string, key: Sort | null, cls = ""): HTMLElement => {
      if (!key) return h("span", `picker__th ${cls}`.trim(), label);
      const b = h("button", `picker__th picker__th--sort ${cls}`.trim(), label);
      b.type = "button";
      b.tabIndex = -1;
      b.classList.toggle("is-active", sort === key);
      b.addEventListener("click", () => setSort(key));
      return b;
    };
    thead.append(col("session", "name"), col("directory", null), col("activity", "activity"),
      col("cpu", "cpu", "picker__th--use"), col("memory", "memory", "picker__th--use"), col("disk", "disk", "picker__th--use"),
      col("last opened", "opened"), col("created", "created"), h("span"));
  };

  const updateCount = (): void => {
    const count = (st: State): number => sessions.filter((s) => stateOf(s) === st).length;
    const asking = count("asking");
    const finished = sessions.filter((s) => s.attention?.level === "done").length;
    const needs = [asking ? `${asking} need${asking > 1 ? "" : "s"} you` : "",
      finished ? `${finished} finished` : ""].filter(Boolean);
    statusNeeds.textContent = needs.join(" · ");
    statusNeeds.hidden = !loaded || !needs.length;
    needsBreak.hidden = statusNeeds.hidden;
    statusNeeds.dataset.level = asking ? "ask" : "done";
    clearDone.hidden = !loaded || !finished;
    const working = count("working");
    const tasks = count("task");
    const open = count("open");
    const parts = [`${sessions.length} running`];
    if (working) parts.push(`${working} working`);
    if (tasks) parts.push(`${tasks} with a task`);
    if (open) parts.push(`${open} open`);
    if (stopped.length) parts.push(`${stopped.length} stopped`);
    statusCount.textContent = loaded ? parts.join(" · ") : "";
  };

  const stoppedWrap = h("section", "picker__stopped");
  stoppedWrap.hidden = true;
  const stoppedHead = h("div", "picker__stopped__head");
  const stoppedTitle = h("span", "picker__stopped__title");
  const stoppedHint = h("span", "picker__stopped__hint", "not running — tap one to resume it");
  const forgetAll = h("button", "picker__stopped__forget");
  forgetAll.type = "button";
  stoppedHead.append(stoppedTitle, stoppedHint, forgetAll);
  const stoppedList = h("div", "picker__list");
  stoppedWrap.append(stoppedHead, stoppedList);
  let forgetArmed: number | null = null;
  const disarmForget = (): void => {
    forgetAll.classList.remove("armed");
    forgetAll.textContent = "forget all…";
    if (forgetArmed !== null) { clearTimeout(forgetArmed); forgetArmed = null; }
  };
  forgetAll.addEventListener("click", async () => {
    const shown = stopped.filter((st) => matches(asInfo(st), query));
    if (forgetArmed === null) {
      forgetAll.classList.add("armed");
      forgetAll.textContent = `confirm: forget ${shown.length}`;
      forgetArmed = window.setTimeout(disarmForget, 5000);
      return;
    }
    disarmForget();
    for (const st of shown) {
      try {
        await apiJson(`/api/sessions/${encodeURIComponent(st.name)}/forget`, { method: "POST" });
      } catch (err) {
        showError(`forget ${st.name} failed: ${(err as Error).message}`);
        break;
      }
    }
    void load("full");
  });
  cleanups.push(() => { if (forgetArmed !== null) clearTimeout(forgetArmed); });

  const render = (resort: boolean): void => {
    for (const t of killTimers) clearTimeout(t);
    killTimers.clear();
    syncControls();
    if (resort || order.length === 0) {
      order = [...sessions].sort((a, b) => needsYouFirst(a, b) || SORTERS[sort](a, b)).map((s) => s.name);
    } else {
      // Keep the order the user is looking at; new sessions go on top.
      const idx = new Map(order.map((n, i) => [n, i]));
      order = [...sessions]
        .sort((a, b) => (idx.get(a.name) ?? -1) - (idx.get(b.name) ?? -1) || SORTERS[sort](a, b))
        .map((s) => s.name);
    }
    const byName = new Map(sessions.map((s) => [s.name, s]));
    const shown = order.map((n) => byName.get(n)!).filter((s) => matches(s, query));
    const shownStopped = stopped
      .map((st) => ({ st, info: asInfo(st) }))
      .filter(({ info }) => matches(info, query))
      .sort((a, b) => SORTERS[sort](a.info, b.info));
    maxCpu = Math.max(0, ...shown.map((s) => s.cpu ?? 0));
    maxMem = Math.max(0, ...shown.map((s) => s.mem ?? 0));
    maxDisk = Math.max(0, ...shown.map((s) => s.disk ?? 0), ...shownStopped.map(({ st }) => st.disk ?? 0));

    observer?.disconnect();
    previewTargets.clear();
    visible.clear();
    list.className = `picker__list picker__list--${view}`;
    stoppedList.className = `picker__list picker__list--${view}`;
    thead.hidden = view !== "detailed" || (shown.length === 0 && shownStopped.length === 0);
    if (!thead.hidden) renderThead();
    updateCount();
    list.replaceChildren();
    stoppedList.replaceChildren();
    stoppedWrap.hidden = true;

    if (!loaded) return;
    if (sessions.length === 0 && stopped.length === 0) {
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
      list.append(h("div", "picker__nomatch",
        sessions.length ? `no running session matches “${query}”` : "no sessions running"));
    } else {
      shown.forEach((s, i) => list.append(renderItem(s, i)));
    }
    if (shownStopped.length) {
      stoppedWrap.hidden = false;
      stoppedTitle.textContent = `stopped · ${shownStopped.length}`;
      disarmForget();
      shownStopped.forEach(({ st, info }, i) => stoppedList.append(renderItem(info, shown.length + i, st)));
    }
    firstPaint = false;
    if (view === "grid") schedulePreviews(60);
  };

  // A soft refresh doesn't rebuild the list while the user is in the middle
  // of something on it.
  // Keyboard focus on a row holds the refresh while the keyboard is in use
  // (a rebuild would drop the focus). A click leaves focus on whatever it
  // hit for as long as nothing else is clicked — that alone must not
  // freeze the list, so it doesn't count (not :focus-visible).
  const keyboardInList = (): boolean => {
    const a = document.activeElement as HTMLElement | null;
    if (!a || !(list.contains(a) || stoppedWrap.contains(a))) return false;
    return a.matches(":focus-visible") && Date.now() - lastKey < 30_000;
  };
  const interacting = (): boolean =>
    Date.now() - lastPointer < 1500
    || keyboardInList()
    || !!inner.querySelector(".sess__menu:not([hidden]), .sess__rename, .sess.is-resuming");

  let loadSeq = 0;
  const load = async (mode: "full" | "soft"): Promise<void> => {
    const mine = ++loadSeq;
    let fresh: SessionInfo[];
    let rem: StoppedInfo[];
    try {
      [fresh, rem] = await Promise.all([
        apiJson<SessionInfo[]>("/api/sessions"),
        // An older backend has no remembered list.
        apiJson<StoppedInfo[]>("/api/sessions/remembered").catch(() => [] as StoppedInfo[]),
      ]);
    } catch (err) {
      if (mode === "full") showError(`failed to list sessions: ${(err as Error).message}`);
      return;
    }
    if (mine !== loadSeq || !frame.isConnected) return;
    if (mode === "soft" && interacting()) return;
    if (mode === "full" && errBox.textContent?.startsWith("failed to list")) showError("");
    sessions = fresh;
    stopped = Array.isArray(rem) ? rem : [];
    loaded = true;
    const mem = sessions.reduce((t, s) => t + (s.mem ?? 0), 0);
    sysPanel.setSessionsMem(sessions.some((s) => s.mem != null) ? mem : null, sessions.length);
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

  // ─── What needs you (attention.ts) ──────────────────────────────────────
  // /api/sessions carries it too; this checks more often, and its check-in
  // tells the server this device has ccpipe on screen (so it isn't pushed to).
  let attention: AttentionItem[] = [];
  let attentionKey = "";
  let attentionDirty = false;
  const newAttention = new NewItems();
  const applyAttention = (): void => {
    for (const s of sessions) {
      s.attention = attention.find((i) => i.session === s.name && i.level === "ask")
        ?? attention.find((i) => i.session === s.name && i.level === "done") ?? null;
    }
  };
  const pollAttention = async (): Promise<void> => {
    const visible = document.visibilityState === "visible";
    let items: AttentionItem[];
    try {
      ({ items } = await apiJson<{ items: AttentionItem[] }>(
        `/api/attention?device=${encodeURIComponent(deviceId())}&visible=${visible ? 1 : 0}`));
    } catch { return; }
    if (!frame.isConnected || !Array.isArray(items)) return;
    attention = items;
    const fresh = newAttention.take(items);
    const key = items.map((i) => `${i.id}:${i.body}`).join("\n");
    if (key !== attentionKey || attentionDirty) {
      attentionKey = key;
      // Not under a finger: the next check paints it.
      if (loaded && !interacting()) { applyAttention(); render(false); attentionDirty = false; }
      else attentionDirty = true;
    }
    setTitleCount(items.length);
    setAppBadge(items.length);
    void tidyNotifications(items);
    // Which session it is: a banner naming it (the row may be out of sight,
    // and rows don't move while you're looking), the row flashes, and after
    // the chime its name is said.
    const prefs = loadAlerts();
    const audible = canSound();
    let rang = false;
    for (const it of fresh) {
      if (!pushCovers(it.level)) {
        fireResponseReady(it.body, it.session, it.level === "ask" ? it.title : "Claude finished");
      }
      if (!document.hidden) {
        toast(it, () => onPick(it.session));
        flashRow(it);
      }
      const want = prefs[it.level];
      if (!audible || !(want.sound || want.speak)) continue;
      const ring = want.sound && !rang;
      if (ring) rang = true;
      // Once per device, whichever ccpipe page gets there first.
      void claimAlert(it).then((mine) => {
        if (!mine) return;
        if (ring) chime(it.level);
        if (want.speak) void sayName(it);
      });
    }
  };
  const flashRow = (it: AttentionItem): void => {
    const row = list.querySelector<HTMLElement>(`.sess[data-name="${CSS.escape(it.session)}"]`);
    if (!row) return;
    row.classList.remove("sess--flash");
    row.style.animation = "";   // the first paint's slide-in would win
    void row.offsetWidth;     // start it again
    row.classList.add("sess--flash");
    row.dataset.flash = it.level;
    const end = (e: AnimationEvent): void => {
      if (e.target !== row) return;           // the dot's, or a child's
      row.classList.remove("sess--flash");
      row.removeEventListener("animationend", end);
    };
    row.addEventListener("animationend", end);
  };
  clearDone.addEventListener("click", async (e) => {
    e.stopPropagation();
    try {
      await apiJson("/api/attention/dismiss", { method: "POST", body: JSON.stringify({}) });
    } catch { /* the next check shows what's still there */ }
    void pollAttention();
  });
  const attentionTimer = window.setInterval(() => { void pollAttention(); }, ATTENTION_POLL_MS);
  cleanups.push(() => clearInterval(attentionTimer), installChimeUnlock(), () => setTitleCount(0),
    clearToasts);
  void pollAttention();

  // ─── Document-level wiring (removed on teardown) ────────────────────────
  list.addEventListener("pointerdown", () => { lastPointer = Date.now(); });
  stoppedWrap.addEventListener("pointerdown", () => { lastPointer = Date.now(); });
  listen("click", (e) => {
    const t = e.target as Element | null;
    if (!t?.closest?.(".sess__actions")) closeMenus();
  });
  listen("keydown", (e) => {
    lastKey = Date.now();
    if (e.key === "Escape") { closeMenus(); return; }
    if (e.key !== "/" || e.ctrlKey || e.metaKey || e.altKey) return;
    const a = document.activeElement as HTMLElement | null;
    if (a && (a.tagName === "INPUT" || a.tagName === "TEXTAREA" || a.tagName === "SELECT" || a.isContentEditable)) return;
    if (document.querySelector(".modal-overlay")) return;
    e.preventDefault();
    searchInput.focus();
  });
  listen("visibilitychange", () => {
    // Hidden too: the server stops counting this device as looking.
    void pollAttention();
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
  inner.append(stoppedWrap);
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
