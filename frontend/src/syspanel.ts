// The landing page's system panel: CPU and memory with ten minutes of
// history, the root filesystem as a ring, and how much of the Claude plan's
// usage limits is used (a ring, with a tick for how much of the window has
// gone) — plus how much of the memory ccpipe's sessions are using. A
// compact strip: each card is the number on the left and its graph on the
// right. Hidden with Settings → Display → "system panel on the landing page".

import { h } from "./dom";
import {
  SysPoller, type StatsPrefs, type SysSnapshot, fmtAgo, fmtBytes, fmtPct, fmtWhen, level, loadStatsPrefs,
  onStatsPrefs, planHeadline, planLevel, planPace, planResets, ring, sparkline,
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
  const plan = card("plan", "sp-card--disk sp-card--plan");
  el.append(cpu.el, mem.el, disk.el, plan.el);
  let prefs = loadStatsPrefs();

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
    paintPlan();
  };

  // The headline meter (the server's pick: on this account the all-models
  // week); the others are in a session's top-bar pop-up and the tooltip.
  const paintPlan = (): void => {
    const p = prefs.plan ? last?.plan : null;
    const m = planHeadline(p);
    plan.el.hidden = !m;
    el.classList.toggle("sp--4", !!m);
    if (!p || !m) return;
    const now = p.now;
    const lvl = planLevel(m);
    plan.value.textContent = fmtPct(m.percent);
    plan.value.dataset.level = lvl;
    plan.sub.textContent = m.resets_at && !m.reset ? `${m.label} · resets ${fmtWhen(m.resets_at, now)}` : m.label;
    plan.viz.replaceChildren(ring(m.percent, "", planPace(m, now), lvl));
    const lines = p.meters.map((x) => {
      const pace = planPace(x, now);
      return `${x.label}: ${fmtPct(x.percent)} · ${planResets(x, now)}`
        + (pace !== null ? ` · ${Math.round(pace)}% of the window gone (the tick)` : "");
    });
    if (p.at) lines.push(`as of ${fmtAgo(Math.max(0, now - p.at))}`);
    plan.el.title = `Claude plan usage\n${lines.join("\n")}`;
  };

  const poller = new SysPoller(null, loadStatsPrefs().interval, paint);
  const apply = (p: StatsPrefs): void => {
    prefs = p;
    paintPlan();
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
