// The ping's drop-down: a graph of the round trips, their spread, and the
// connection's ups and downs. In a session the ping times this session's
// WebSocket (a ping frame and its pong, ws.ts); on the landing page it
// times a request for /api/health. The readings collect in a LatencyLog,
// and while the drop-down is open its owner measures every couple of
// seconds instead of at its usual pace, so the graph moves.

import { h } from "./dom";
import { dropdown } from "./dropdown";
import { fmtAgo, fmtSpan, sparkHeight, sparkline } from "./sysviz";

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
  /** When it went down, while it's down (including never having come up). */
  downSince: number | null = null;
  /** Times it went down after being up. */
  drops = 0;
  /** The last time it went down after being up, and for how long once it's back. */
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
    this.downSince = null;
    if (this.lastDrop && this.lastDrop.downFor === null) this.lastDrop.downFor = now - this.lastDrop.at;
    this.emit();
  }

  /** Disconnected, or a check failed. A drop only counts after being up;
   * before that it's just down, since the first failure. */
  offline(): void {
    const now = Date.now();
    if (this.up) {
      this.up = false;
      this.drops += 1;
      this.lastDrop = { at: now, downFor: null };
      this.downSince = now;
      this.push({ at: now, ms: null });
    } else if (this.downSince === null) {
      this.downSince = now;
      this.emit();
    }
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
  if (s < 10) return `${s.toFixed(1)} s`;
  if (s < 60) return `${Math.round(s)} s`;
  if (s < 3600) return `${Math.round(s / 60)} min`;
  return fmtSpan(s);
}

/** "2 s", "30 s", "1 min". */
const fmtEvery = (ms: number): string => (ms < 60_000 ? `${ms / 1000} s` : `${ms / 60_000} min`);

// The graph's top: the first of these at or above the highest reading,
// with a little headroom. Never under 50 ms, so a quiet home network's
// few milliseconds of wobble sit flat along the bottom rather than
// filling the box.
const SCALES = [50, 100, 200, 300, 500, 1000, 2000, 5000, 10000];
const scaleFor = (max: number): number => SCALES.find((s) => s >= max * 1.15) ?? SCALES[SCALES.length - 1];

// ─── the drop-down ────────────────────────────────────────────────────────

export interface PingDropdownOptions {
  /** The connection section's title. */
  linkTitle: string;
  /** What a drop is called here: "reconnects", "outages". */
  dropsLabel: string;
  /** "wss", "ws", "https" or "http". */
  scheme: string;
  /** What's timed, for the note: "A ping goes to ccpipe …". */
  what: string;
  /** How often it's timed while the drop-down is shut, in ms. */
  everyMs: number;
  /** Opened (true) or closed: measure every FAST_PING_MS meanwhile. */
  onOpenChange(open: boolean): void;
  /** See DropdownOptions.keepFocus. */
  keepFocus?: boolean;
}

export interface PingDropdown {
  dispose(): void;
}

interface Cell { k: HTMLElement; v: HTMLElement; sub: HTMLElement }

function cell(grid: HTMLElement, label: string): Cell {
  const c = h("div", "sysmeter-pop__cell");
  const k = h("span", "sysmeter-pop__k", label);
  const v = h("span", "sysmeter-pop__v", "—");
  const sub = h("span", "sysmeter-pop__sub");
  sub.hidden = true;
  c.append(k, v, sub);
  grid.append(c);
  return { k, v, sub };
}

/** Text and colour (a latencyLevel, or none). */
function show(el: HTMLElement, text: string, level = ""): void {
  el.textContent = text;
  if (level) el.dataset.ping = level;
  else delete el.dataset.ping;
}

function showSub(c: Cell, text: string): void {
  c.sub.textContent = text;
  c.sub.hidden = !text;
}

export function pingDropdown(trigger: HTMLElement, log: LatencyLog, opts: PingDropdownOptions): PingDropdown {
  const secure = opts.scheme === "wss" || opts.scheme === "https";
  const note = `${secure ? "Encrypted" : "Not encrypted"} (${opts.scheme}). ${opts.what} every `
    + `${fmtEvery(opts.everyMs)}, and every ${fmtEvery(FAST_PING_MS)} while this is open.`;
  let ticker: number | null = null;
  let unsub: (() => void) | null = null;

  // Built once when it opens, then updated in place: a reading rebuilds
  // only the graph, and the clock ticks only the times, so a hover tooltip
  // or a selection in it survives.
  const build = (pop: HTMLElement): { update(): void; tick(): void } => {
    const head = h("div", "sysmeter-pop__head");
    const nowV = h("span", "sysmeter-pop__v");
    head.append(h("span", "sysmeter-pop__k", "round trip"), nowV);
    const viz = h("div", "sysmeter-pop__viz pingpop__viz");
    const span = h("div", "sysmeter-pop__sub");
    const top = h("div", "sysmeter-pop__row");
    top.append(head, viz, span);

    const spread = h("div", "sysmeter-pop__grid sysmeter-pop__grid--4 pingpop__stats");
    spread.title = "jitter: how much one reading differs from the next, on average";
    const minC = cell(spread, "min");
    const medC = cell(spread, "median");
    const maxC = cell(spread, "max");
    const jitC = cell(spread, "jitter");

    const link = h("div", "sysmeter-pop__mine");
    const grid = h("div", "sysmeter-pop__grid");
    const upC = cell(grid, "up for");
    const dropsC = cell(grid, opts.dropsLabel);
    const lastC = cell(grid, "last drop");
    link.append(h("div", "sysmeter-pop__title", opts.linkTitle), grid,
      h("div", "sysmeter-pop__sub pingpop__note", note));

    const key = h("div", "sysmeter-key__levels pingpop__key");
    for (const [lvl, text] of [["ok", "under 100 ms"], ["warn", "100 ms+"], ["bad", "300 ms+"]]) {
      const item = h("span", "sysmeter-key__level");
      const sw = h("span", "sysmeter-key__swatch pingpop__swatch");
      sw.dataset.ping = lvl;
      item.append(sw, text);
      key.append(item);
    }
    pop.replaceChildren(top, spread, link, key);

    let count = 0;

    const graph = (max: number): void => {
      const scale = scaleFor(max);
      const vals = log.readings.map((r) => (r.ms === null ? null : (r.ms / scale) * 100));
      // The readings so far fill the width (a page's first few would be a
      // sliver at the edge of a 150-reading box); at least 20 slots, so two
      // readings don't stretch to a full-width line.
      const kids: Element[] = [sparkline(vals, "sv-spark--ping", Math.max(20, vals.length))];
      // Guides where the ping changes colour, when they're inside the scale.
      for (const t of [100, 300]) {
        if (t >= scale) continue;
        const line = h("span", `pingpop__guide pingpop__guide--${latencyLevel(t)}`);
        line.style.bottom = `${sparkHeight((t / scale) * 100).toFixed(1)}%`;
        line.append(h("span", "pingpop__guide-label", String(t)));
        kids.push(line);
      }
      kids.push(h("span", "pingpop__scale", `${scale} ms`));
      viz.replaceChildren(...kids);
    };

    const tick = (): void => {
      const now = Date.now();
      const first = log.readings[0];
      span.textContent = count && first
        ? `last ${count} reading${count === 1 ? "" : "s"} · over ${fmtFor(now - first.at)}`
        : "waiting for the first reading…";
      if (log.up && log.since !== null) {
        upC.k.textContent = "up for";
        show(upC.v, fmtFor(now - log.since));
        showSub(upC, "");
      } else if (log.downSince !== null) {
        upC.k.textContent = "down for";
        show(upC.v, fmtFor(now - log.downSince), "bad");
        showSub(upC, "");
      } else {
        upC.k.textContent = "up for";
        show(upC.v, "—");
        showSub(upC, "connecting…");
      }
      const ld = log.lastDrop;
      show(lastC.v, ld ? fmtAgo((now - ld.at) / 1000) : "none");
      showSub(lastC, ld && ld.downFor !== null ? `out ${fmtFor(ld.downFor)}` : "");
    };

    const update = (): void => {
      const cur = log.current();
      if (cur !== null) show(nowV, fmtMs(cur), latencyLevel(cur));
      else if (!log.up && log.downSince !== null) show(nowV, "down", "bad");
      else show(nowV, "—");
      const st = log.stats();
      count = st?.count ?? 0;
      spread.hidden = !st;
      if (st) {
        show(minC.v, fmtMs(st.min));
        show(medC.v, fmtMs(st.median));
        show(maxC.v, fmtMs(st.max), latencyLevel(st.max));
        show(jitC.v, st.jitter !== null ? fmtMs(st.jitter) : "—");
      }
      graph(st?.max ?? 0);
      show(dropsC.v, String(log.drops));
      tick();
    };

    return { update, tick };
  };

  const dd = dropdown(trigger, {
    label: "Connection",
    cls: "sysmeter-pop pingpop",
    keepFocus: opts.keepFocus,
    onOpen(pop) {
      const view = build(pop);
      view.update();
      unsub = log.subscribe(view.update);
      // The "up for" and "ago" times move on between readings.
      ticker = window.setInterval(view.tick, 1000);
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
