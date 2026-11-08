// What needs you, on the page: Claude's questions and its finished turns
// in every session (the server finds them: backend attention.py).
//
//   ask   a question or a permission prompt. Urgent: a toast, a chime, and
//         the session's name said aloud.
//   done  Claude finished a turn while no page showed the session. Quiet:
//         a softer chime, nothing said.
//
// Which of these happen is chosen per device (Settings → Notifications) and
// kept in this browser. Push notifications, for when no page is on screen,
// are push.ts.

import { h } from "./dom";
import { dropdown } from "./dropdown";
import { ICON_BELL, ICON_X, iconNode } from "./icons";

export type Level = "ask" | "done";

export type AttentionItem = {
  id: number;
  session: string;
  level: Level;
  kind: "question" | "permission" | "done";
  /** "Claude has a question", "Claude has 2 questions", "Claude finished". */
  title: string;
  /** The question and its options, or the end of Claude's reply. */
  body: string;
  /** Unix seconds. */
  since: number;
};

// ─── This device's choices ────────────────────────────────────────────────

export type AlertPrefs = Record<Level, { sound: boolean; speak: boolean }>;

const PREFS_KEY = "ccpipe.alerts";
const PREFS_EVENT = "ccpipe:alerts";
export const DEFAULT_ALERTS: AlertPrefs = {
  ask: { sound: true, speak: true },
  done: { sound: true, speak: false },
};

export function loadAlerts(): AlertPrefs {
  const out: AlertPrefs = { ask: { ...DEFAULT_ALERTS.ask }, done: { ...DEFAULT_ALERTS.done } };
  try {
    const raw = JSON.parse(localStorage.getItem(PREFS_KEY) || "{}");
    for (const lv of ["ask", "done"] as const) {
      for (const k of ["sound", "speak"] as const) {
        if (typeof raw?.[lv]?.[k] === "boolean") out[lv][k] = raw[lv][k];
      }
    }
  } catch { /* private mode, or junk: the defaults */ }
  return out;
}

export function saveAlerts(p: AlertPrefs): void {
  try { localStorage.setItem(PREFS_KEY, JSON.stringify(p)); } catch { /* private mode */ }
  window.dispatchEvent(new CustomEvent(PREFS_EVENT));
}

/** Calls `cb` when this tab or another changes the choices. */
export function onAlertsChange(cb: (p: AlertPrefs) => void): () => void {
  const local = () => cb(loadAlerts());
  const other = (e: StorageEvent) => { if (e.key === PREFS_KEY) cb(loadAlerts()); };
  window.addEventListener(PREFS_EVENT, local);
  window.addEventListener("storage", other);
  return () => {
    window.removeEventListener(PREFS_EVENT, local);
    window.removeEventListener("storage", other);
  };
}

/** A random id this browser keeps: the server uses it to tell whether a
 * ccpipe page is on this device's screen (then it doesn't push to it). */
export function deviceId(): string {
  const KEY = "ccpipe.device";
  try {
    const have = localStorage.getItem(KEY);
    if (have && /^[A-Za-z0-9_-]{8,64}$/.test(have)) return have;
    const b = new Uint8Array(12);
    crypto.getRandomValues(b);
    const id = Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
    localStorage.setItem(KEY, id);
    return id;
  } catch {
    return "";
  }
}

// ─── Words ────────────────────────────────────────────────────────────────

export function whatLabel(item: AttentionItem): string {
  if (item.level === "done") return "finished";
  if (item.kind === "permission") return "needs permission";
  return /\d+ questions/.test(item.title) ? item.title.replace(/^Claude /, "") : "has a question";
}

/** A question is answered; a permission prompt or a finished turn opened. */
function actionLabel(item: AttentionItem): string {
  return item.kind === "question" ? "Answer" : "Open";
}

export function ago(since: number): string {
  const s = Math.max(0, Math.floor(Date.now() / 1000 - since));
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

/** Which items are new to this page: the ones the server says just started,
 * and (after a reconnect, or between two polls) ones not seen before that
 * started in the last two minutes. The first list a page gets isn't new. */
export class NewItems {
  private seen = new Set<number>();
  private primed = false;

  take(items: AttentionItem[], newIds: number[] = []): AttentionItem[] {
    const now = Date.now() / 1000;
    const fresh = items.filter((i) => !this.seen.has(i.id)
      && (newIds.includes(i.id) || (this.primed && now - i.since < 120)));
    this.seen = new Set(items.map((i) => i.id));
    this.primed = true;
    return fresh;
  }
}

/** Whether this page may make a sound now. A hidden page on a phone or
 * tablet stays quiet (Safari lets one page play, and it should be the one
 * in front); a desktop tab behind others can still chime. */
export function canSound(): boolean {
  return !document.hidden || !matchMedia("(pointer: coarse)").matches;
}

/** The first ccpipe page on this device to ask gets to ring (and say) an
 * item; the others stay quiet, however many tabs are open. */
export async function claimAlert(item: AttentionItem): Promise<boolean> {
  const KEY = "ccpipe.alerted";
  // Ids start again when the server restarts: the time keeps them apart.
  const id = `${item.id}-${item.since}`;
  const claim = (): boolean => {
    try {
      const now = Date.now();
      const seen = JSON.parse(localStorage.getItem(KEY) || "{}") as Record<string, number>;
      for (const k of Object.keys(seen)) if (now - seen[k] > 10 * 60_000) delete seen[k];
      if (seen[id]) return false;
      seen[id] = now;
      localStorage.setItem(KEY, JSON.stringify(seen));
      return true;
    } catch {
      return true;          // no storage: ring rather than miss it
    }
  };
  const locks = (navigator as Navigator & { locks?: LockManager }).locks;
  if (!locks) return claim();
  try {
    return await locks.request("ccpipe-alerts", () => claim());
  } catch {
    return claim();
  }
}

// ─── The chime ────────────────────────────────────────────────────────────
// Made here (no sound files): two rising notes for a question, one soft
// one for a finished turn, then (if chosen) the session's name in
// ccpipe's voice.
//
// Both play through Web Audio, not an <audio> element. Safari builds the
// lock screen's player (and the iPad's play button) from media elements,
// and playing one stops the music or podcast that was on: a chime that
// way took over the player, and pressing play replayed the tone. Web
// Audio mixes with what's playing and stays off the player. Like any web
// game's sound, it follows the device's silent mode.
//
// Safari lets the context run once a tap or key press has started it, so
// every page starts it on its first one. If it still won't run, a session
// page falls back to its voice player's element (setChimeFallback).

const notesOf: Record<Level, { notes: Array<[number, number, number]>; gain: number }> = {
  ask: { notes: [[659.25, 0, 0.5], [987.77, 0.13, 0.75]], gain: 0.3 },
  done: { notes: [[587.33, 0, 0.7]], gain: 0.16 },
};

function chimePcm(level: Level, rate: number): Float32Array<ArrayBuffer> {
  const { notes, gain } = notesOf[level];
  const len = Math.ceil(rate * Math.max(...notes.map(([, s, d]) => s + d)));
  const pcm = new Float32Array(len);
  for (const [f, start, dur] of notes) {
    const s0 = Math.floor(start * rate);
    const n = Math.floor(dur * rate);
    for (let i = 0; i < n && s0 + i < len; i++) {
      const t = i / rate;
      const env = Math.min(1, t / 0.004) * Math.exp(-t * 6);
      pcm[s0 + i] += env * gain * (Math.sin(2 * Math.PI * f * t) + 0.25 * Math.sin(4 * Math.PI * f * t));
    }
  }
  return pcm;
}

/** The chime as a WAV, for the fallback. */
function chimeBlob(level: Level): Blob {
  const rate = 22050;
  const pcm = chimePcm(level, rate);
  const len = pcm.length;
  const buf = new ArrayBuffer(44 + len * 2);
  const v = new DataView(buf);
  const tag = (o: number, t: string) => { for (let i = 0; i < t.length; i++) v.setUint8(o + i, t.charCodeAt(i)); };
  tag(0, "RIFF"); v.setUint32(4, 36 + len * 2, true); tag(8, "WAVE");
  tag(12, "fmt "); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, rate, true); v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
  tag(36, "data"); v.setUint32(40, len * 2, true);
  for (let i = 0; i < len; i++) v.setInt16(44 + i * 2, Math.max(-1, Math.min(1, pcm[i])) * 0x7fff, true);
  return new Blob([buf], { type: "audio/wav" });
}

let ctx: AudioContext | null = null;
let noContext = false;

function context(): AudioContext | null {
  if (ctx || noContext) return ctx;
  const AC = window.AudioContext
    ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
  try {
    if (AC) ctx = new AC();
  } catch { /* none */ }
  noContext = !ctx;
  return ctx;
}

const isRunning = (c: AudioContext): boolean => (c.state as string) === "running";

/** Inside a tap or key press: start the context (Safari's condition), with
 * a silent frame, which iOS also wants. Cheap once it runs. */
function unlockAudio(): void {
  const c = context();
  if (!c || isRunning(c)) return;
  c.resume().catch(() => {});
  try {
    const src = c.createBufferSource();
    src.buffer = c.createBuffer(1, 1, c.sampleRate);
    src.connect(c.destination);
    src.start(0);
  } catch { /* the next tap tries again */ }
}

const GESTURES = ["pointerup", "touchend", "click", "keydown"] as const;

/** Every page with alerts: a tap or key press lets the chime play. */
export function installChimeUnlock(): () => void {
  for (const g of GESTURES) document.addEventListener(g, unlockAudio, { capture: true, passive: true });
  return () => { for (const g of GESTURES) document.removeEventListener(g, unlockAudio, { capture: true }); };
}

/** Running now, or after a short try (a backgrounded iPad page comes back
 * "interrupted"; Chrome and Firefox resume a page you've used). */
async function ready(c: AudioContext): Promise<boolean> {
  if (isRunning(c)) return true;
  try {
    await Promise.race([c.resume(), new Promise((r) => window.setTimeout(r, 400))]);
  } catch { /* still not running */ }
  return isRunning(c);
}

let fallback: ((blob: Blob) => Promise<void>) | null = null;
let busy: () => boolean = () => false;
let note: (msg: string) => void = () => {};

/** A session page: where the chime and the name go if Web Audio won't run
 * (its voice player's element, the one Safari lets it play), whether its
 * voice is speaking (a chime then would talk over it), and where to report
 * which way it played (the server log, for a device we can't see). */
export function setChimeFallback(opts: {
  play: (blob: Blob) => Promise<void>; busy: () => boolean; log: (msg: string) => void;
}): () => void {
  fallback = opts.play;
  busy = opts.busy;
  note = opts.log;
  return () => {
    if (fallback !== opts.play) return;
    fallback = null;
    busy = () => false;
    note = () => {};
  };
}

// One after another: the chime, then the name. `chain` keeps them in the
// order asked; `endsAt` is when the last one scheduled finishes.
let chain: Promise<void> = Promise.resolve();
let endsAt = 0;

function play(what: string, make: (c: AudioContext) => Promise<AudioBuffer> | AudioBuffer,
              blob: () => Blob): void {
  chain = chain.then(async () => {
    const c = context();
    if (c && await ready(c)) {
      try {
        const buf = await make(c);
        const src = c.createBufferSource();
        src.buffer = buf;
        src.connect(c.destination);
        const at = Math.max(c.currentTime + 0.02, endsAt);
        src.start(at);
        endsAt = at + buf.duration + 0.12;
        return;
      } catch (e) {
        note(`${what}: web audio failed (${(e as Error)?.name ?? e})`);
      }
    }
    if (fallback) {
      note(`${what}: voice player (audio context ${c ? c.state : "missing"})`);
      // It settles as it starts: the next one waits for it to finish
      // (busy while the voice player plays), or it would cut it off.
      await fallback(blob());
      for (let w = 0; busy() && w < 15_000; w += 150) {
        await new Promise((r) => window.setTimeout(r, 150));
      }
    }
  }).catch(() => {});
}

export function chime(level: Level): void {
  if (busy()) return;
  play(`chime ${level}`, (c) => {
    const pcm = chimePcm(level, c.sampleRate);
    const buf = c.createBuffer(1, pcm.length, c.sampleRate);
    buf.copyToChannel(pcm, 0);
    return buf;
  }, () => chimeBlob(level));
}

/** After the chime: which session it is ("infra has a question"). The
 * words are the server's, and a device hears each item once, whichever
 * page asks first (204 when it won't, or the voice is set to read
 * nothing). On a session page, after its voice has finished speaking. */
export async function sayName(item: AttentionItem): Promise<void> {
  let bytes: ArrayBuffer;
  try {
    const res = await fetch("/api/attention/say", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-Requested-By": "ccpipe" },
      body: JSON.stringify({ id: item.id, device: deviceId() }),
    });
    if (res.status !== 200) return;
    bytes = await res.arrayBuffer();
  } catch {
    return;
  }
  if (!bytes.byteLength) return;
  for (let waited = 0; busy() && waited < 30_000; waited += 300) {
    await new Promise((r) => window.setTimeout(r, 300));
  }
  if (busy()) return;
  // decodeAudioData takes the buffer over: the fallback keeps its own copy.
  const copy = bytes.slice(0);
  play("name", (c) => c.decodeAudioData(bytes), () => new Blob([copy], { type: "audio/mpeg" }));
}

/** For Settings: hear what each sounds like (inside the tap, so it plays). */
export function previewChime(level: Level): void {
  unlockAudio();
  chime(level);
}

// ─── Taps that leave the terminal's focus alone ───────────────────────────

/** Like a click, but pressing doesn't move the focus, so the terminal (and
 * the phone's keyboard) keeps it — as for the top bar's buttons
 * (dropdown.ts). Cancelling touchstart cancels the click too, so a touch
 * counts on pointerup, and only if the finger stayed put: the end of a
 * swipe or a drag isn't a tap. */
export function onTap(el: HTMLElement, fn: (e: Event) => void): void {
  el.addEventListener("mousedown", (e) => e.preventDefault());
  el.addEventListener("touchstart", (e) => e.preventDefault(), { passive: false });
  let touchedAt = 0;
  let down: { x: number; y: number } | null = null;
  el.addEventListener("pointerdown", (e) => { down = { x: e.clientX, y: e.clientY }; });
  el.addEventListener("pointercancel", () => { down = null; });
  el.addEventListener("pointerup", (e) => {
    if (e.pointerType === "mouse") return;
    const start = down;
    down = null;
    if (!start || Math.hypot(e.clientX - start.x, e.clientY - start.y) > 10) return;
    touchedAt = Date.now();
    fn(e);
  });
  el.addEventListener("click", (e) => {
    e.stopPropagation();
    if (Date.now() - touchedAt < 700) return;
    fn(e);
  });
}

/** A button in the bell's list, which scrolls: an ordinary click, so a
 * swipe that starts on it scrolls the list. (Each of them leaves the
 * session or changes the list, so the focus moving doesn't matter.) */
function onClick(el: HTMLElement, fn: () => void): void {
  el.addEventListener("click", (e) => { e.stopPropagation(); fn(); });
}

// ─── Toasts ───────────────────────────────────────────────────────────────

let toastBox: HTMLElement | null = null;
const TOAST_MS: Record<Level, number> = { ask: 12_000, done: 6_000 };

function toastHost(): HTMLElement {
  if (toastBox?.isConnected) return toastBox;
  toastBox = h("div", "attn-toasts");
  toastBox.setAttribute("aria-live", "polite");
  document.body.append(toastBox);
  return toastBox;
}

/** A banner under the top bar: who needs you and why, with Open. */
export function toast(item: AttentionItem, onOpen: () => void): void {
  const host = toastHost();
  // Under a session's top bar; at the top on the session list (styles.css).
  const bar = document.querySelector(".statusbar");
  host.style.top = bar ? `${Math.round(bar.getBoundingClientRect().bottom + 8)}px` : "";
  host.querySelector(`[data-key="${item.level}-${CSS.escape(item.session)}"]`)?.remove();
  const el = h("div", "attn-toast");
  el.dataset.level = item.level;
  el.dataset.key = `${item.level}-${item.session}`;
  el.setAttribute("role", "status");
  const text = h("div", "attn-toast__text");
  const head = h("div", "attn-toast__head");
  head.append(h("span", "attn-toast__dot"), h("span", "attn-toast__name", item.session),
    h("span", "attn-toast__what", whatLabel(item)));
  text.append(head);
  if (item.body) text.append(h("div", "attn-toast__body", item.body));
  const open = h("button", "attn-toast__open", actionLabel(item));
  open.type = "button";
  const close = h("button", "attn-toast__close");
  close.type = "button";
  close.title = "Dismiss";
  close.append(iconNode(ICON_X));
  el.append(text, open, close);
  let timer = 0;
  const remove = () => { window.clearTimeout(timer); el.remove(); };
  onTap(open, () => { remove(); onOpen(); });
  onTap(text, () => { remove(); onOpen(); });
  onTap(close, remove);
  timer = window.setTimeout(remove, TOAST_MS[item.level]);
  host.prepend(el);
  while (host.children.length > 3) host.lastElementChild?.remove();
}

export function clearToasts(): void {
  toastBox?.remove();
  toastBox = null;
}

// ─── The bell, in a session's top bar ─────────────────────────────────────

export interface Bell {
  readonly el: HTMLElement;
  /** What needs you in the other sessions (this one is on screen). */
  update(items: AttentionItem[]): void;
  dispose(): void;
}

export function attentionBell(opts: {
  onOpen(session: string): void;
  /** Show it as a tile beside this session, where tiles fit (canTile). */
  onTile?: (session: string) => void;
  canTile?: () => boolean;
  /** Clear a finished turn (a session), or all of them (null). */
  onDismiss(session: string | null): void;
}): Bell {
  const btn = h("button", "pill pill--icon attn-bell");
  btn.type = "button";
  btn.hidden = true;
  const count = h("span", "attn-bell__count");
  btn.append(iconNode(ICON_BELL), count);
  let items: AttentionItem[] = [];
  let ticker = 0;

  const paint = (pop: HTMLElement): void => {
    pop.replaceChildren();
    const head = h("div", "sysmeter-pop__head");
    head.append(h("span", "sysmeter-pop__title", "Other sessions"));
    pop.append(head);
    if (!items.length) {
      pop.append(h("div", "attn-pop__empty", "Nothing needs you."));
      return;
    }
    for (const it of items) {
      const row = h("div", "attn-row");
      row.dataset.level = it.level;
      const top = h("div", "attn-row__head");
      top.append(h("span", "attn-row__dot"), h("span", "attn-row__name", it.session),
        h("span", "attn-row__what", whatLabel(it)), h("span", "attn-row__age", ago(it.since)));
      row.append(top);
      if (it.body) row.append(h("div", "attn-row__body", it.body));
      const acts = h("div", "attn-row__acts");
      const open = h("button", "btn btn--small", actionLabel(it));
      open.type = "button";
      onClick(open, () => { dd.close(); opts.onOpen(it.session); });
      acts.append(open);
      if (opts.onTile && (opts.canTile?.() ?? true)) {
        const tile = h("button", "btn btn--small btn--ghost", "Beside this");
        tile.type = "button";
        tile.title = `Show ${it.session} as a tile beside this session`;
        onClick(tile, () => { dd.close(); opts.onTile?.(it.session); });
        acts.append(tile);
      }
      if (it.level === "done") {
        const clr = h("button", "btn btn--small btn--ghost", "Clear");
        clr.type = "button";
        onClick(clr, () => opts.onDismiss(it.session));
        acts.append(clr);
      }
      row.append(acts);
      pop.append(row);
    }
    if (items.filter((i) => i.level === "done").length > 1) {
      const all = h("button", "btn btn--small btn--ghost attn-pop__clear", "Clear all finished");
      all.type = "button";
      onClick(all, () => opts.onDismiss(null));
      pop.append(all);
    }
  };

  const dd = dropdown(btn, {
    label: "Sessions that need you",
    cls: "sysmeter-pop attn-pop",
    width: 360,
    onOpen: (pop) => {
      clearToasts();      // the list says it all, and they'd cover it
      paint(pop);
      ticker = window.setInterval(() => { if (dd.pop) paint(dd.pop); }, 30_000);
    },
    onClose: () => { window.clearInterval(ticker); },
  });

  return {
    el: btn,
    update(next) {
      items = next;
      const asks = next.filter((i) => i.level === "ask").length;
      btn.hidden = next.length === 0;
      btn.dataset.level = asks ? "ask" : "done";
      count.textContent = String(next.length);
      const names = next.map((i) => `${i.session} ${whatLabel(i)}`);
      btn.title = names.length ? names.join("\n") : "";
      if (dd.pop) {
        if (next.length) paint(dd.pop);
        else dd.close();
      }
    },
    dispose() {
      dd.close();
      clearToasts();
    },
  };
}

// ─── The tab's title and the app icon's badge ─────────────────────────────

export function setTitleCount(n: number): void {
  const base = document.title.replace(/^\(\d+\) /, "");
  document.title = n > 0 ? `(${n}) ${base}` : base;
}

export function setAppBadge(n: number): void {
  const nav = navigator as Navigator & {
    setAppBadge?: (n?: number) => Promise<void>;
    clearAppBadge?: () => Promise<void>;
  };
  try {
    if (n > 0) void nav.setAppBadge?.(n)?.catch(() => {});
    else void nav.clearAppBadge?.()?.catch(() => {});
  } catch { /* not supported */ }
}
