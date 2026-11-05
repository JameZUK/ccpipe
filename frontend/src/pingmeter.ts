// The ping's drop-down: a graph of the round trips, their spread, and the
// connection's ups and downs. In a session the ping times this session's
// WebSocket (a ping frame and its pong, ws.ts); on the landing page it
// times a request for /api/health. The readings collect in a LatencyLog,
// and while the drop-down is open its owner measures every couple of
// seconds instead of at its usual pace, so the graph moves.

import { h } from "./dom";
import { dropdown } from "./dropdown";
import { fmtAgo, fmtSpan, sparkline } from "./sysviz";

/** How often to measure while the drop-down is open. */
export const FAST_PING_MS = 2000;

/** The colours the ping already wears: green under 100 ms, amber to 300,
 * red beyond. */
export function latencyLevel(ms: number): "ok" | "warn" | "bad" {
  return ms < 100 ? "ok" : ms < 300 ? "warn" : "bad";
}

export interface Reading {
  /** Date.now() when it came in. */
  at: number;
  /** The round trip, or null for a break: the connection went down there. */
  ms: number | null;
}

export interface LatencyStats {
  count: number;
  min: number;
  median: number;
  max: number;
  /** The average change from one reading to the next. */
  jitter: number | null;
  /** ms from the oldest reading to now. */
  span: number;
}

/** Round trips and the connection's history, for one page view. */
export class LatencyLog {
  static readonly CAP = 150;
  /** Oldest first. */
  readonly readings: Reading[] = [];
  /** Connected now (or the last check was answered). */
  up = false;
  /** When it last came up, or null before it first has. */
  since: number | null = null;
  /** Times it went down after being up. */
  drops = 0;
  /** The last time it went down, and for how long once it's back. */
  lastDrop: { at: number; downFor: number | null } | null = null;
  private readonly subs = new Set<() => void>();

  add(ms: number): void {
    this.push({ at: Date.now(), ms });
  }

  /** Connected, or a check was answered. */
  online(): void {
    if (this.up) return;
    const now = Date.now();
    this.up = true;
    this.since = now;
    if (this.lastDrop && this.lastDrop.downFor === null) this.lastDrop.downFor = now - this.lastDrop.at;
    this.emit();
  }

  /** Disconnected, or a check failed. Only counts after being up. */
  offline(): void {
    if (!this.up) return;
    const now = Date.now();
    this.up = false;
    this.drops += 1;
    this.lastDrop = { at: now, downFor: null };
    this.push({ at: now, ms: null });
  }

  /** The newest reading while up; null while down or before the first. */
  current(): number | null {
    const r = this.readings[this.readings.length - 1];
    return this.up && r ? r.ms : null;
  }

  stats(): LatencyStats | null {
    const vals = this.readings.flatMap((r) => (r.ms === null ? [] : [r.ms]));
    if (!vals.length) return null;
    const sorted = [...vals].sort((a, b) => a - b);
    const mid = sorted.length >> 1;
    const median = sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
    // Jitter: the mean step between neighbours, not across a break.
    let steps = 0;
    let total = 0;
    for (let i = 1; i < this.readings.length; i++) {
      const a = this.readings[i - 1].ms;
      const b = this.readings[i].ms;
      if (a === null || b === null) continue;
      total += Math.abs(b - a);
      steps += 1;
    }
    return {
      count: vals.length,
      min: sorted[0],
      median,
      max: sorted[sorted.length - 1],
      jitter: steps ? total / steps : null,
      span: Date.now() - this.readings[0].at,
    };
  }

  subscribe(fn: () => void): () => void {
    this.subs.add(fn);
    return () => this.subs.delete(fn);
  }

  private push(r: Reading): void {
    this.readings.push(r);
    if (this.readings.length > LatencyLog.CAP) this.readings.splice(0, this.readings.length - LatencyLog.CAP);
    this.emit();
  }

  private emit(): void {
    for (const fn of this.subs) {
      try { fn(); } catch { /* one bad listener doesn't stop the rest */ }
    }
  }
}

// ─── formatting ───────────────────────────────────────────────────────────

const fmtMs = (ms: number): string => `${ms < 10 && ms % 1 ? ms.toFixed(1) : Math.round(ms)} ms`;

/** "40 ms", "1.2 s", "45 s", "12 min", "2 h 5 m". */
function fmtFor(ms: number): string {
  if (ms < 1000) return `${Math.max(0, Math.round(ms))} ms`;
  const s = ms / 1000;
  if (s < 10) return `${Math.max(0, s).toFixed(1)} s`;
  if (s < 60) return `${Math.round(s)} s`;
  if (s < 3600) return `${Math.round(s / 60)} min`;
  return fmtSpan(s);
}

// The graph's top: the first of these at or above the highest reading,
// with a little headroom. Never under 50 ms, so a quiet home network's
// few milliseconds of wobble sit flat along the bottom rather than
// filling the box.
const SCALES = [50, 100, 200, 300, 500, 1000, 2000, 5000, 10000];
const scaleFor = (max: number): number => SCALES.find((s) => s >= max * 1.15) ?? SCALES[SCALES.length - 1];

// Where a value sits up the graph, as a share of its height: sysviz's
// sparkline draws 0–100 % between y = 31 and y = 2 of a 32-high box.
const heightAt = (pct: number): number => ((1 + 0.29 * Math.max(0, Math.min(100, pct))) / 32) * 100;

// ─── the drop-down ────────────────────────────────────────────────────────

export interface PingDropdownOptions {
  /** The connection section's title. */
  linkTitle: string;
  /** What a drop is called here: "reconnects", "failed checks". */
  dropsLabel: string;
  /** "wss", "https"… */
  scheme: string;
  /** Whether that's over TLS. */
  secure: boolean;
  /** A note on what's timed, and how often. */
  note: string;
  /** Opened (true) or closed: measure every FAST_PING_MS meanwhile. */
  onOpenChange(open: boolean): void;
}

export interface PingDropdown {
  dispose(): void;
}

export function pingDropdown(trigger: HTMLElement, log: LatencyLog, opts: PingDropdownOptions): PingDropdown {
  let ticker: number | null = null;
  let unsub: (() => void) | null = null;

  const cell = (grid: HTMLElement, k: string, v: string, sub = "", level = ""): void => {
    const c = h("div", "sysmeter-pop__cell");
    const val = h("span", "sysmeter-pop__v", v);
    if (level) val.dataset.ping = level;
    c.append(h("span", "sysmeter-pop__k", k), val);
    if (sub) c.append(h("span", "sysmeter-pop__sub", sub));
    grid.append(c);
  };

  const graph = (): HTMLElement => {
    const viz = h("div", "sysmeter-pop__viz pingpop__viz");
    const vals = log.readings.map((r) => r.ms);
    const st = log.stats();
    const scale = scaleFor(st?.max ?? 0);
    // The readings so far fill the width (a page's first few would be a
    // sliver at the edge of a 150-reading box); at least 20 slots, so two
    // readings don't stretch to a full-width line.
    viz.append(sparkline(vals.map((v) => (v === null ? null : (v / scale) * 100)), "sv-spark--ping",
      Math.max(20, vals.length)));
    // Guides where the ping changes colour, when they're inside the scale.
    for (const t of [100, 300]) {
      if (t >= scale) continue;
      const line = h("span", `pingpop__guide pingpop__guide--${latencyLevel(t)}`);
      line.style.bottom = `${heightAt((t / scale) * 100).toFixed(1)}%`;
      line.append(h("span", "pingpop__guide-label", String(t)));
      viz.append(line);
    }
    viz.append(h("span", "pingpop__scale", `${scale} ms`));
    return viz;
  };

  const paint = (): void => {
    const pop = dd.pop;
    if (!pop) return;
    const now = Date.now();
    const cur = log.current();
    const st = log.stats();

    const top = h("div", "sysmeter-pop__row");
    const head = h("div", "sysmeter-pop__head");
    const v = h("span", "sysmeter-pop__v", cur !== null ? fmtMs(cur) : log.up ? "—" : "down");
    v.dataset.ping = cur !== null ? latencyLevel(cur) : log.up ? "" : "bad";
    head.append(h("span", "sysmeter-pop__k", "round trip"), v);
    top.append(head, graph());
    top.append(h("div", "sysmeter-pop__sub",
      st ? `last ${st.count} reading${st.count === 1 ? "" : "s"} · over ${fmtFor(st.span)}` : "waiting for the first reading…"));

    const spread = h("div", "sysmeter-pop__grid sysmeter-pop__grid--4 pingpop__stats");
    if (st) {
      cell(spread, "min", fmtMs(st.min));
      cell(spread, "median", fmtMs(st.median));
      cell(spread, "max", fmtMs(st.max), "", latencyLevel(st.max));
      cell(spread, "jitter", st.jitter !== null ? fmtMs(st.jitter) : "—");
      spread.title = "jitter: how much one reading differs from the next, on average";
    }

    const link = h("div", "sysmeter-pop__mine");
    link.append(h("div", "sysmeter-pop__title", opts.linkTitle));
    const grid = h("div", "sysmeter-pop__grid");
    if (log.up && log.since !== null) cell(grid, "up for", fmtFor(now - log.since));
    else if (!log.up && log.lastDrop) cell(grid, "down for", fmtFor(now - log.lastDrop.at), "", "bad");
    else cell(grid, "up for", "—", "connecting…");
    cell(grid, opts.dropsLabel, String(log.drops));
    const ld = log.lastDrop;
    if (ld) {
      cell(grid, "last drop", fmtAgo((now - ld.at) / 1000),
        ld.downFor !== null ? `out ${fmtFor(ld.downFor)}` : "");
    } else {
      cell(grid, "last drop", "none");
    }
    link.append(grid, h("div", "sysmeter-pop__sub pingpop__note",
      `${opts.secure ? "Encrypted" : "Not encrypted"} (${opts.scheme}). ${opts.note}`));

    const key = h("div", "sysmeter-key__levels pingpop__key");
    for (const [lvl, text] of [["ok", "under 100 ms"], ["warn", "100 ms+"], ["bad", "300 ms+"]]) {
      const item = h("span", "sysmeter-key__level");
      const sw = h("span", "sysmeter-key__swatch pingpop__swatch");
      sw.dataset.ping = lvl;
      item.append(sw, text);
      key.append(item);
    }

    pop.replaceChildren(top, ...(st ? [spread] : []), link, key);
  };

  const dd = dropdown(trigger, {
    label: "Connection",
    cls: "sysmeter-pop pingpop",
    onOpen() {
      paint();
      unsub = log.subscribe(paint);
      // The "up for" and "ago" times move on between readings.
      ticker = window.setInterval(paint, 1000);
      opts.onOpenChange(true);
    },
    onClose() {
      unsub?.();
      unsub = null;
      if (ticker !== null) clearInterval(ticker);
      ticker = null;
      opts.onOpenChange(false);
    },
  });

  return {
    dispose() { dd.close(); },
  };
}
