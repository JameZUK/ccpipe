// System-status visuals shared by the session picker and the session bar:
// a sparkline, a ring gauge and a bar, all thin amber strokes with the
// spark mark's soft glow (styles.css .sv-*), plus formatting, a poller for
// /api/system and the per-device display preferences.
//
// Built with DOM APIs only (createElementNS) — nothing is parsed as HTML.

import { svgEl } from "./dom";

export interface SysSnapshot {
  interval: number;
  at: number | null;
  cpu: { pct: number | null; cores: number; load: number[] } | null;
  mem: {
    total: number | null; used: number | null; available: number | null; pct: number | null;
    swap_total: number; swap_used: number;
  } | null;
  disk: { path: string; total?: number; used?: number; free?: number } | null;
  history: { cpu: (number | null)[]; mem: (number | null)[] };
  session?: { name: string; cpu: number | null; mem: number | null; cwd: string | null; disk: number | null };
}

// ─── preferences (per device) ─────────────────────────────────────────────

export interface StatsPrefs {
  /** System panel on the landing page. */
  landing: boolean;
  /** Stats next to the ping in a session's top bar. */
  sessionBar: boolean;
  /** RAM / disk per session on the landing page. */
  perSession: boolean;
  /** Seconds between refreshes. */
  interval: number;
}
const PREFS_KEY = "ccpipe.stats";
export const STATS_INTERVALS = [2, 5, 10, 30] as const;
const DEFAULT_PREFS: StatsPrefs = { landing: true, sessionBar: true, perSession: true, interval: 5 };

export function loadStatsPrefs(): StatsPrefs {
  try {
    const raw = JSON.parse(localStorage.getItem(PREFS_KEY) || "{}") as Partial<StatsPrefs>;
    return {
      landing: typeof raw.landing === "boolean" ? raw.landing : DEFAULT_PREFS.landing,
      sessionBar: typeof raw.sessionBar === "boolean" ? raw.sessionBar : DEFAULT_PREFS.sessionBar,
      perSession: typeof raw.perSession === "boolean" ? raw.perSession : DEFAULT_PREFS.perSession,
      interval: (STATS_INTERVALS as readonly number[]).includes(raw.interval as number)
        ? (raw.interval as number) : DEFAULT_PREFS.interval,
    };
  } catch {
    return { ...DEFAULT_PREFS };
  }
}

export function saveStatsPrefs(p: StatsPrefs): void {
  try { localStorage.setItem(PREFS_KEY, JSON.stringify(p)); } catch { /* private mode */ }
  window.dispatchEvent(new CustomEvent("ccpipe:stats-prefs", { detail: p }));
}

export function onStatsPrefs(cb: (p: StatsPrefs) => void): () => void {
  const fn = (e: Event): void => cb((e as CustomEvent<StatsPrefs>).detail);
  window.addEventListener("ccpipe:stats-prefs", fn);
  return () => window.removeEventListener("ccpipe:stats-prefs", fn);
}

// ─── formatting ───────────────────────────────────────────────────────────

export function fmtBytes(n: number | null | undefined): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = n;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v >= 100 || i === 0 ? Math.round(v) : v.toFixed(1)} ${units[i]}`;
}

export const fmtPct = (p: number | null | undefined): string =>
  p === null || p === undefined ? "—" : `${Math.round(p)}%`;

/** ok < 70 % ≤ warn < 90 % ≤ hot */
export function level(pct: number | null | undefined): "ok" | "warn" | "hot" {
  if (pct === null || pct === undefined) return "ok";
  return pct >= 90 ? "hot" : pct >= 70 ? "warn" : "ok";
}

// ─── visuals ──────────────────────────────────────────────────────────────

const svg = svgEl;
let gradSeq = 0;

/** A 0–100 % series as a line with a fading fill. Gaps (null) break it.
 * The newest value sits at the right edge and a series shorter than
 * `capacity` fills in from the right, like a scrolling monitor. */
export function sparkline(values: (number | null)[], cls = "", capacity = 120): SVGSVGElement {
  const el = svg("svg", { class: `sv-spark ${cls}`.trim(), viewBox: "0 0 100 32", preserveAspectRatio: "none" });
  el.setAttribute("aria-hidden", "true");
  values = values.slice(-capacity);
  const n = values.length;
  if (n < 2) return el;
  const slots = Math.max(capacity, n) - 1;
  const offset = slots - (n - 1);
  const id = `svg-${++gradSeq}`;
  const defs = svg("defs", {});
  const grad = svg("linearGradient", { id, x1: "0", y1: "0", x2: "0", y2: "1" });
  grad.append(svg("stop", { offset: "0", class: "sv-spark__stop-a" }), svg("stop", { offset: "1", class: "sv-spark__stop-b" }));
  defs.append(grad);
  el.append(defs);
  const pts: string[] = [];
  let line = "";
  let area = "";
  const flush = (): void => {
    if (pts.length >= 2) {
      line += `M${pts.join("L")}`;
      const first = pts[0].split(",")[0];
      const last = pts[pts.length - 1].split(",")[0];
      area += `M${first},32L${pts.join("L")}L${last},32Z`;
    }
    pts.length = 0;
  };
  values.forEach((v, i) => {
    if (v === null || v === undefined) { flush(); return; }
    const x = ((offset + i) / slots) * 100;
    const y = 31 - (Math.max(0, Math.min(100, v)) / 100) * 29;
    pts.push(`${x.toFixed(2)},${y.toFixed(2)}`);
  });
  flush();
  el.append(svg("path", { d: area, class: "sv-spark__area", fill: `url(#${id})` }),
    svg("path", { d: line, class: "sv-spark__line", "vector-effect": "non-scaling-stroke" }));
  return el;
}

/** A ring filled to pct. */
export function ring(pct: number | null, cls = ""): SVGSVGElement {
  const el = svg("svg", { class: `sv-ring sv--${level(pct)} ${cls}`.trim(), viewBox: "0 0 36 36" });
  el.setAttribute("aria-hidden", "true");
  const r = 15.5;
  const c = 2 * Math.PI * r;
  el.append(svg("circle", { class: "sv-ring__track", cx: "18", cy: "18", r: String(r) }));
  const arc = svg("circle", { class: "sv-ring__fill", cx: "18", cy: "18", r: String(r),
    "stroke-dasharray": `${((Math.max(0, Math.min(100, pct ?? 0)) / 100) * c).toFixed(2)} ${c.toFixed(2)}` });
  el.append(arc);
  return el;
}

/** A horizontal bar filled to pct (0–100). */
export function bar(pct: number | null, cls = ""): HTMLElement {
  const el = document.createElement("span");
  el.className = `sv-bar sv--${level(pct)} ${cls}`.trim();
  el.setAttribute("aria-hidden", "true");
  const fill = document.createElement("span");
  fill.className = "sv-bar__fill";
  fill.style.width = `${Math.max(0, Math.min(100, pct ?? 0))}%`;
  el.append(fill);
  return el;
}

// ─── poller ───────────────────────────────────────────────────────────────

/** Polls /api/system every `seconds` while the page is visible. */
/** Polls /api/system while active (and the page is visible). Pause it with
 * setActive(false) when its display is switched off — a hidden panel must
 * not keep asking the server (which also keeps the per-session sampling
 * awake). stop() is for good. */
export class SysPoller {
  private timer: number | null = null;
  private active = false;
  private disposed = false;
  // Bumped on every pause / resume, so a fetch already in flight neither
  // reports into a paused display nor starts a second timer chain.
  private gen = 0;
  private readonly onVis = (): void => { if (!document.hidden) void this.tick(); };

  constructor(
    private readonly session: string | null,
    private seconds: number,
    private readonly cb: (s: SysSnapshot) => void,
  ) {}

  setActive(on: boolean): void {
    if (this.disposed || on === this.active) return;
    this.active = on;
    this.gen += 1;
    if (on) {
      document.addEventListener("visibilitychange", this.onVis);
      void this.tick();
    } else {
      this.clearTimer();
      document.removeEventListener("visibilitychange", this.onVis);
    }
  }

  start(): void { this.setActive(true); }

  setInterval(seconds: number): void {
    this.seconds = seconds;
    if (this.active) this.schedule();
  }

  stop(): void {
    this.setActive(false);
    this.disposed = true;
  }

  private clearTimer(): void {
    if (this.timer !== null) { clearTimeout(this.timer); this.timer = null; }
  }

  private schedule(): void {
    this.clearTimer();
    if (this.active) {
      this.timer = window.setTimeout(() => { void this.tick(); }, this.seconds * 1000);
    }
  }

  private async tick(): Promise<void> {
    if (!this.active) return;
    const gen = this.gen;
    if (document.hidden) { this.schedule(); return; }
    try {
      const q = this.session ? `?session=${encodeURIComponent(this.session)}` : "";
      const res = await fetch(`/api/system${q}`, { credentials: "same-origin", cache: "no-store" });
      if (res.ok && gen === this.gen) this.cb(await res.json() as SysSnapshot);
    } catch { /* offline: next tick */ }
    if (gen === this.gen) this.schedule();
  }
}
