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
  session?: {
    name: string; cpu: number | null; mem: number | null; cwd: string | null; disk: number | null;
    /** The context window's fill, from the session's last status-line reading. */
    context?: { percent: number; window: number | null; model: string | null; at: number } | null;
  };
  /** The Claude subscription's usage limits (backend plan_usage.py); null when off. */
  plan?: PlanUsage | null;
}

/** One usage meter, as the server reports it (weekly all models, weekly per
 * model, the 5-hour session window on plans that have one). */
export interface PlanMeter {
  kind: string;
  group: string;
  label: string;
  percent: number;
  /** Unix seconds. */
  resets_at: number | null;
  /** The window's length in seconds, for the pace marker; null if unknown. */
  window: number | null;
  severity: string;
  /** The server's pick for a single-number readout. */
  active: boolean;
  source: "usage" | "statusline";
  at: number;
  /** Its reset time has passed since the reading. */
  reset?: boolean;
}

export interface PlanUsage {
  meters: PlanMeter[];
  /** Usage credits; amounts in minor units (pence, cents). */
  credits: { enabled: boolean; limit: number | null; used: number | null; pct: number | null;
    currency: string | null } | null;
  at: number | null;
  usage_at: number | null;
  statusline_at: number | null;
  checking: boolean;
  error: string | null;
  /** The server's clock, for the times above. */
  now: number;
}

// ─── preferences (per device) ─────────────────────────────────────────────

export interface StatsPrefs {
  /** System panel on the landing page. */
  landing: boolean;
  /** Stats next to the ping in a session's top bar. */
  sessionBar: boolean;
  /** RAM / disk per session on the landing page. */
  perSession: boolean;
  /** Claude plan usage in the top bar, its pop-up and the landing page. */
  plan: boolean;
  /** Seconds between refreshes. */
  interval: number;
}
const PREFS_KEY = "ccpipe.stats";
export const STATS_INTERVALS = [2, 5, 10, 30] as const;
const DEFAULT_PREFS: StatsPrefs = { landing: true, sessionBar: true, perSession: true, plan: true, interval: 5 };

export function loadStatsPrefs(): StatsPrefs {
  try {
    const raw = JSON.parse(localStorage.getItem(PREFS_KEY) || "{}") as Partial<StatsPrefs>;
    return {
      landing: typeof raw.landing === "boolean" ? raw.landing : DEFAULT_PREFS.landing,
      sessionBar: typeof raw.sessionBar === "boolean" ? raw.sessionBar : DEFAULT_PREFS.sessionBar,
      perSession: typeof raw.perSession === "boolean" ? raw.perSession : DEFAULT_PREFS.perSession,
      plan: typeof raw.plan === "boolean" ? raw.plan : DEFAULT_PREFS.plan,
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

// ─── Claude plan usage ────────────────────────────────────────────────────

/** The meter to show as one number: the server's pick, else the first. */
export function planHeadline(p: PlanUsage | null | undefined): PlanMeter | null {
  if (!p?.meters.length) return null;
  return p.meters.find((m) => m.active) ?? p.meters[0];
}

const LEVEL_RANK = { ok: 0, warn: 1, hot: 2 } as const;

/** The meter's colour: its percentage's level, or the server's severity
 * when that's worse. */
export function planLevel(m: PlanMeter): "ok" | "warn" | "hot" {
  const own = level(m.percent);
  const sev = m.severity === "critical" ? "hot" : m.severity === "warning" ? "warn" : "ok";
  return LEVEL_RANK[sev] > LEVEL_RANK[own] ? sev : own;
}

/** How much of the meter's window has gone by (0–100), or null. Usage
 * ahead of it is on course to run out before the reset. */
export function planPace(m: PlanMeter, now: number): number | null {
  if (!m.window || !m.resets_at || m.reset) return null;
  const left = m.resets_at - now;
  if (left <= 0 || left > m.window) return null;
  return (1 - left / m.window) * 100;
}

const DAY_FMT = new Intl.DateTimeFormat(undefined, { weekday: "short" });
const TIME_FMT = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" });

/** "16:40", "tomorrow 06:00", "Fri 06:00" — local time. */
export function fmtWhen(at: number, now: number): string {
  const d = new Date(at * 1000);
  const today = new Date(now * 1000);
  const days = Math.round((new Date(d).setHours(0, 0, 0, 0) - new Date(today).setHours(0, 0, 0, 0)) / 86_400_000);
  const time = TIME_FMT.format(d);
  if (days === 0) return time;
  if (days === 1) return `tomorrow ${time}`;
  return `${DAY_FMT.format(d)} ${time}`;
}

/** "2 d 14 h", "3 h 20 m", "12 m". */
export function fmtSpan(seconds: number): string {
  const m = Math.max(0, Math.round(seconds / 60));
  if (m < 60) return `${m} m`;
  const h = Math.floor(m / 60);
  if (h < 48) return m % 60 && h < 10 ? `${h} h ${m % 60} m` : `${h} h`;
  return `${Math.floor(h / 24)} d ${h % 24} h`;
}

/** "just now", "4 min ago", "2 h ago". */
export function fmtAgo(seconds: number): string {
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 48 * 3600) return `${Math.round(seconds / 3600)} h ago`;
  return `${Math.round(seconds / 86400)} days ago`;
}

export function fmtMoney(minor: number | null, currency: string | null): string {
  if (minor === null) return "—";
  try {
    return new Intl.NumberFormat(undefined, { style: "currency", currency: currency || "USD" }).format(minor / 100);
  } catch {
    return `${(minor / 100).toFixed(2)} ${currency ?? ""}`.trim();
  }
}

/** When the meter resets: "resets Fri 06:00 · in 2 d 14 h". */
export function planResets(m: PlanMeter, now: number): string {
  if (m.reset) return "reset since the last reading";
  if (!m.resets_at) return "";
  return `resets ${fmtWhen(m.resets_at, now)} · in ${fmtSpan(m.resets_at - now)}`;
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

/** A ring filled to pct, with a tick at `pace` (0–100) when given. Its
 * colour is `lvl`, or pct's level. */
export function ring(pct: number | null, cls = "", pace: number | null = null,
  lvl: "ok" | "warn" | "hot" = level(pct)): SVGSVGElement {
  const el = svg("svg", { class: `sv-ring sv--${lvl} ${cls}`.trim(), viewBox: "0 0 36 36" });
  el.setAttribute("aria-hidden", "true");
  const r = 15.5;
  const c = 2 * Math.PI * r;
  el.append(svg("circle", { class: "sv-ring__track", cx: "18", cy: "18", r: String(r) }));
  const arc = svg("circle", { class: "sv-ring__fill", cx: "18", cy: "18", r: String(r),
    "stroke-dasharray": `${((Math.max(0, Math.min(100, pct ?? 0)) / 100) * c).toFixed(2)} ${c.toFixed(2)}` });
  el.append(arc);
  if (pace !== null) {
    const a = (Math.max(0, Math.min(100, pace)) / 100) * 2 * Math.PI;
    const at = (k: number): [string, string] => [(18 + k * Math.cos(a)).toFixed(2), (18 + k * Math.sin(a)).toFixed(2)];
    const [x1, y1] = at(r - 3.6);
    const [x2, y2] = at(r + 3.6);
    el.append(svg("line", { class: "sv-ring__pace", x1, y1, x2, y2 }));
  }
  return el;
}

/** A horizontal bar filled to pct (0–100), coloured `lvl` (default: pct's level). */
export function bar(pct: number | null, cls = "", lvl: "ok" | "warn" | "hot" = level(pct)): HTMLElement {
  const el = document.createElement("span");
  el.className = `sv-bar sv--${lvl} ${cls}`.trim();
  el.setAttribute("aria-hidden", "true");
  const fill = document.createElement("span");
  fill.className = "sv-bar__fill";
  fill.style.width = `${Math.max(0, Math.min(100, pct ?? 0))}%`;
  el.append(fill);
  return el;
}

/** A plan meter's bar, with a tick where the window's elapsed share is:
 * a fill past the tick is using the plan faster than an even pace. */
export function paceBar(m: PlanMeter, now: number, cls = ""): HTMLElement {
  const wrap = document.createElement("span");
  wrap.className = `sv-pbar ${cls}`.trim();
  wrap.append(bar(m.percent, "", planLevel(m)));
  const pace = planPace(m, now);
  if (pace !== null) {
    const tick = document.createElement("span");
    tick.className = "sv-pbar__pace";
    tick.style.left = `${pace.toFixed(1)}%`;
    wrap.append(tick);
  }
  wrap.setAttribute("aria-hidden", "true");
  return wrap;
}

/** Ask the server to check the plan figures again (the pop-up opened).
 * Resolves to its answer, or null. */
export async function refreshPlan(): Promise<{ checking: boolean; plan: PlanUsage | null } | null> {
  try {
    const res = await fetch("/api/plan-usage/refresh", {
      method: "POST", credentials: "same-origin", headers: { "X-Requested-By": "ccpipe" },
    });
    return res.ok ? await res.json() as { checking: boolean; plan: PlanUsage | null } : null;
  } catch {
    return null;
  }
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
