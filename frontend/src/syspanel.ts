// The landing page's system panel: CPU and memory with ten minutes of
// history, and the root filesystem as a ring — plus how much of the memory
// ccpipe's sessions are using. A compact strip: each card is the number on
// the left and its graph on the right. Hidden with Settings → Display →
// "system panel on the landing page".

import { h } from "./dom";
import {
  SysPoller, type StatsPrefs, type SysSnapshot, fmtBytes, fmtPct, level, loadStatsPrefs, onStatsPrefs, ring,
  sparkline,
} from "./sysviz";

export interface SysPanel {
  el: HTMLElement;
  /** Memory used by all sessions (sum of their PSS), from /api/sessions. */
  setSessionsMem(bytes: number | null, count: number): void;
  dispose(): void;
}


interface Card { el: HTMLElement; value: HTMLElement; sub: HTMLElement; viz: HTMLElement }

function card(label: string, cls = ""): Card {
  const el = h("div", `sp-card ${cls}`.trim());
  const text = h("div", "sp-card__text");
  const value = h("span", "sp-card__value", "—");
  const sub = h("span", "sp-card__sub");
  text.append(h("span", "sp-card__label", label), value, sub);
  const viz = h("div", "sp-card__viz");
  el.append(text, viz);
  return { el, value, sub, viz };
}

export function mountSysPanel(): SysPanel {
  const el = h("section", "sp");
  el.setAttribute("aria-label", "System status");
  const cpu = card("cpu");
  const mem = card("mem");
  const disk = card("disk", "sp-card--disk");
  el.append(cpu.el, mem.el, disk.el);

  let sessionsMem: number | null = null;
  let sessionsCount = 0;
  let last: SysSnapshot | null = null;

  const paintMemSub = (): void => {
    const m = last?.mem;
    if (!m?.total) return;
    const parts = [`${fmtBytes(m.used)} / ${fmtBytes(m.total)}`];
    if (sessionsMem !== null) parts.push(`sessions ${fmtBytes(sessionsMem)}`);
    mem.sub.textContent = parts.join(" · ");
    mem.el.title = sessionsMem !== null
      ? `${sessionsCount} session${sessionsCount === 1 ? "" : "s"} use ${fmtBytes(sessionsMem)} `
        + `(${fmtPct((sessionsMem / m.total) * 100)} of memory)`
      : "";
  };

  const paint = (s: SysSnapshot): void => {
    last = s;
    const c = s.cpu?.pct ?? null;
    cpu.value.textContent = fmtPct(c);
    cpu.value.dataset.level = level(c);
    cpu.sub.textContent = s.cpu
      ? `${s.cpu.cores} cores${s.cpu.load?.length ? ` · load ${s.cpu.load[0].toFixed(2)}` : ""}` : "";
    cpu.viz.replaceChildren(sparkline(s.history.cpu));

    mem.value.textContent = fmtPct(s.mem?.pct);
    mem.value.dataset.level = level(s.mem?.pct);
    mem.viz.replaceChildren(sparkline(s.history.mem, "sv-spark--mem"));
    paintMemSub();

    const d = s.disk;
    const dpct = d?.total ? ((d.used ?? 0) / d.total) * 100 : null;
    disk.value.textContent = d?.free != null ? fmtBytes(d.free) : "—";
    disk.value.dataset.level = level(dpct);
    disk.sub.textContent = d?.total ? `free of ${fmtBytes(d.total)} · ${fmtPct(dpct)} used` : "";
    disk.viz.replaceChildren(ring(dpct));
    el.title = s.at ? `updated ${new Date(s.at * 1000).toLocaleTimeString()}` : "";
  };

  const poller = new SysPoller(null, loadStatsPrefs().interval, paint);
  const apply = (p: StatsPrefs): void => {
    el.hidden = !p.landing;
    poller.setInterval(p.interval);
    poller.setActive(p.landing);
  };
  apply(loadStatsPrefs());
  const unsub = onStatsPrefs(apply);

  return {
    el,
    setSessionsMem(bytes, count) {
      sessionsMem = bytes;
      sessionsCount = count;
      paintMemSub();
    },
    dispose() {
      poller.stop();
      unsub();
    },
  };
}
