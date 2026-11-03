// Self-update: the page notices a new deploy and reloads itself onto it.
//
// Every build writes /version.json ({"build": "<sha> · <time>"}) next to
// index.html, and the bundle knows its own id (__BUILD_ID__). The page asks
// the server which build it serves — at start, every CHECK_MS while visible,
// and whenever the page comes back (visible again, window refocused, back
// online) — and when the two differ it reloads, at a moment that costs the
// user nothing:
//   • on the session picker, straight away;
//   • when the page comes back after being hidden or unfocused for a while
//     (the user hasn't started doing anything yet);
//   • after IDLE_RELOAD_MS with no input at all.
// Never while something is in progress: a dialog, panel or menu is open, a
// text field has focus (the terminal's own input aside), dictation or speech
// is running, or the terminal is scrolled up. Meanwhile a small "reload"
// toast offers it sooner, and Settings' footer button says an update is
// ready.
//
// Until the reload the old page keeps working: the build keeps the last few
// builds' hashed files (vite.config.ts), so a lazily-loaded chunk of the old
// build still loads. Should one be missing anyway, Vite fires
// vite:preloadError and the page reloads — that missing chunk is what left
// the picker blank after a deploy.

const CHECK_MS = 2 * 60_000;
const IDLE_RELOAD_MS = 10 * 60_000;
/** Hidden or unfocused at least this long counts as "coming back". */
const AWAY_MS = 5_000;
/** No reload within this long of the user's last key / tap. */
const QUIET_MS = 3_000;
/** A reload that didn't land on the new build isn't retried for this long. */
const RETRY_GUARD_MS = 10 * 60_000;
const GUARD_KEY = "ccpipe.update.reload";
const CHUNK_GUARD_KEY = "ccpipe.update.chunkReload";

// Anything open whose state a reload would lose. (The picker's "new
// session" panel registers a busy check instead: it opens by itself when
// there are no sessions, and only counts once something is typed in it.)
const BUSY_SELECTOR = [
  ".modal-overlay", ".file-panel-shell", ".md-drawer", ".docs-menu",
  ".prompt-menu", ".tiles-menu__list", ".sess__menu:not([hidden])",
  ".sess__rename", ".select-view", ".file-viewer",
].join(",");

type BusyCheck = () => boolean;
const busyChecks = new Set<BusyCheck>();

let pending: string | null = null;
let reloading = false;
let lastInput = 0;
let awaySince: number | null = null;
let toast: HTMLElement | null = null;
let toastShownFor: string | null = null;
let toastTimer: number | null = null;
const listeners = new Set<(build: string | null) => void>();

/** Register a check that holds off the automatic reload while it returns
 * true (e.g. dictation running). Returns the unregister function. */
export function addBusyCheck(fn: BusyCheck): () => void {
  busyChecks.add(fn);
  return () => { busyChecks.delete(fn); };
}

/** The newer build the server is serving, or null if this page is current. */
export function pendingUpdate(): string | null { return pending; }

/** Be told when an update becomes ready (or goes away). */
export function onUpdateChange(cb: (build: string | null) => void): () => void {
  listeners.add(cb);
  return () => { listeners.delete(cb); };
}

const beforeReload = new Set<() => void>();
/** Run `fn` just before an update reload: close sockets, release audio. */
export function onBeforeReload(fn: () => void): () => void {
  beforeReload.add(fn);
  return () => { beforeReload.delete(fn); };
}

/** Reload now, onto whatever build the server serves. First the page lets
 * go of its connection and audio: on the iPad the old page has been seen
 * to live on after its reload, still connected and still playing. */
export function reloadNow(): void {
  reloading = true;
  for (const fn of beforeReload) { try { fn(); } catch { /* never block the reload */ } }
  location.reload();
}

async function serverBuild(): Promise<string | null> {
  try {
    const res = await fetch("/version.json", { cache: "no-store", credentials: "same-origin" });
    if (!res.ok) return null;
    const body = await res.json() as { build?: unknown };
    return typeof body?.build === "string" ? body.build : null;
  } catch {
    return null;
  }
}

let inFlight: Promise<void> | null = null;
function check(): Promise<void> {
  inFlight ??= serverBuild().then((build) => {
    if (build === null) return;               // offline / mid-deploy: try later
    const next = build !== __BUILD_ID__ ? build : null;
    if (next === pending) return;
    pending = next;
    for (const cb of listeners) { try { cb(pending); } catch { /* listener bug */ } }
    if (pending) showToast(pending); else hideToast();
  }).finally(() => { inFlight = null; });
  return inFlight;
}

function editableFocused(): boolean {
  const a = document.activeElement as HTMLElement | null;
  if (!a || a === document.body) return false;
  // The terminal's own input, and the composer: its draft is on the server
  // 200 ms after the last keystroke (and on hide), well inside QUIET_MS —
  // on the phone it keeps focus, and would otherwise block every reload.
  if (a.classList.contains("xterm-helper-textarea") || a.classList.contains("composer__input")) return false;
  return a.isContentEditable || a.tagName === "TEXTAREA"
    || (a.tagName === "INPUT" && !["checkbox", "radio", "range", "button"].includes((a as HTMLInputElement).type))
    || a.tagName === "SELECT";
}

function busy(): boolean {
  if (document.querySelector(BUSY_SELECTOR)) return true;
  if (editableFocused()) return true;
  for (const fn of busyChecks) {
    try { if (fn()) return true; } catch { /* a broken check doesn't block */ }
  }
  return false;
}

/** The session picker is showing (its frame — session-picker.ts). */
function onPicker(): boolean {
  return !!document.querySelector(".frame--picker");
}

function readGuard(key: string): { build: string; at: number } | null {
  try {
    const v = JSON.parse(sessionStorage.getItem(key) || "null");
    return v && typeof v.build === "string" && typeof v.at === "number" ? v : null;
  } catch { return null; }
}
function writeGuard(key: string, build: string): void {
  try { sessionStorage.setItem(key, JSON.stringify({ build, at: Date.now() })); } catch { /* private mode */ }
}

async function tryApply(): Promise<void> {
  const target = pending;
  if (!target || reloading) return;
  if (busy() || Date.now() - lastInput < QUIET_MS) return;
  // Already reloaded for this build and still not on it (a cache in the
  // way?): don't loop — the toast / Settings button stay for a manual go.
  const g = readGuard(GUARD_KEY);
  if (g && g.build === target && Date.now() - g.at < RETRY_GUARD_MS) return;
  // Only once the new index is really being served: a reload in the middle
  // of a deploy's write would land on the "frontend not built" error.
  try {
    const res = await fetch("/", { cache: "no-store", credentials: "same-origin" });
    if (!res.ok) return;
  } catch { return; }
  if (pending !== target || busy()) return;
  writeGuard(GUARD_KEY, target);
  reloadNow();
}

function showToast(build: string): void {
  if (toastShownFor === build) return;
  toastShownFor = build;
  hideToast();
  toast = document.createElement("div");
  toast.className = "update-toast";
  toast.setAttribute("role", "status");
  const msg = document.createElement("span");
  msg.textContent = "new version ready";
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "update-toast__btn";
  btn.textContent = "reload";
  btn.addEventListener("click", reloadNow);
  const close = document.createElement("button");
  close.type = "button";
  close.className = "update-toast__close";
  close.setAttribute("aria-label", "Dismiss");
  close.textContent = "✕";
  close.addEventListener("click", hideToast);
  toast.append(msg, btn, close);
  document.body.append(toast);
  toastTimer = window.setTimeout(hideToast, 12_000);
}

function hideToast(): void {
  if (toastTimer !== null) { clearTimeout(toastTimer); toastTimer = null; }
  toast?.remove();
  toast = null;
}

/** A lazily-loaded chunk failed to load — almost always because a deploy
 * replaced the build this page came from. Reload onto the new one (at most
 * once a minute, so a genuinely broken chunk can't loop). Returns false
 * when the guard held it back. */
export function recoverFromStaleBundle(): boolean {
  const g = readGuard(CHUNK_GUARD_KEY);
  if (g && Date.now() - g.at < 60_000) return false;
  writeGuard(CHUNK_GUARD_KEY, __BUILD_ID__);
  reloadNow();
  return true;
}

/** Only the chunk-failure recovery — for the standalone pages (/view,
 * /history) that don't need the rest. */
export function installChunkRecovery(): void {
  window.addEventListener("vite:preloadError", () => { recoverFromStaleBundle(); });
}

let installed = false;
export function installUpdater(): void {
  if (installed) return;
  installed = true;
  installChunkRecovery();

  const touched = (): void => { lastInput = Date.now(); };
  for (const ev of ["keydown", "pointerdown", "wheel", "touchstart", "input"]) {
    document.addEventListener(ev, touched, { capture: true, passive: true });
  }

  const away = (): void => { awaySince ??= Date.now(); };
  const back = (): void => {
    const wasAway = awaySince !== null && Date.now() - awaySince >= AWAY_MS;
    awaySince = null;
    if (wasAway) void check().then(tryApply);
  };
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) away(); else back();
  });
  window.addEventListener("blur", away);
  window.addEventListener("focus", back);
  window.addEventListener("pageshow", (e) => { if (e.persisted) void check().then(tryApply); });
  window.addEventListener("online", () => { void check().then(() => { if (onPicker()) void tryApply(); }); });

  window.setInterval(() => {
    if (document.hidden) return;
    void check().then(() => {
      if (onPicker() || Date.now() - lastInput >= IDLE_RELOAD_MS) void tryApply();
    });
  }, CHECK_MS);

  // First check once the page has settled.
  window.setTimeout(() => { void check().then(() => { if (onPicker()) void tryApply(); }); }, 3_000);
}
