// The landing page's system panel: CPU and memory with ten minutes of
// history, and the root filesystem as a ring — plus how much of the memory
// ccpipe's sessions are using. Hidden with Settings → Display → "system
// panel on the landing page".

import {
  SysPoller, type SysSnapshot, bar, fmtBytes, fmtPct, level, loadStatsPrefs, onStatsPrefs, ring, sparkline,
} from "./sysviz";

export interface SysPanel {
  el: HTMLElement;
  /** Memory used by all sessions (sum of their PSS), from /api/sessions. */
  setSessionsMem(bytes: number | null, count: number): void;
  dispose(): void;
}

function h<K extends keyof HTMLElementTagNameMap>(tag: K, cls?: string, text?: string): HTMLElementTagNameMap[K] {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  if (text !== undefined) el.textContent = text;
  return el;
}

function card(label: string): { el: HTMLElement; value: HTMLElement; sub: HTMLElement; viz: HTMLElement; meta: HTMLElement } {
  const el = h("div", "sp-card");
  const head = h("div", "sp-card__head");
  const meta = h("span", "sp-card__meta");
  head.append(h("span", "sp-card__label", label), meta);
  const body = h("div", "sp-card__body");
  const value = h("span", "sp-card__value", "—");
  const sub = h("span", "sp-card__sub");
  body.append(value, sub);
  const viz = h("div", "sp-card__viz");
  el.append(head, body, viz);
  return { el, value, sub, viz, meta };
}

export function mountSysPanel(): SysPanel {
  const el = h("section", "sp");
  el.setAttribute("aria-label", "System status");
  const cpu = card("cpu");
  const mem = card("memory");
  const disk = card("disk");
  disk.el.classList.add("sp-card--disk");
  el.append(cpu.el, mem.el, disk.el);

  let sessionsMem: number | null = null;
  let sessionsCount = 0;
  let last: SysSnapshot | null = null;

  const paint = (s: SysSnapshot): void => {
    last = s;
    const c = s.cpu?.pct ?? null;
    cpu.value.textContent = fmtPct(c);
    cpu.value.dataset.level = level(c);
    cpu.meta.textContent = s.cpu ? `${s.cpu.cores} cores` : "";
    cpu.sub.textContent = s.cpu?.load?.length ? `load ${s.cpu.load[0].toFixed(2)}` : "";
    cpu.viz.replaceChildren(sparkline(s.history.cpu));

    const m = s.mem;
    mem.value.textContent = fmtPct(m?.pct);
    mem.value.dataset.level = level(m?.pct);
    mem.meta.textContent = m?.total ? fmtBytes(m.total) : "";
    mem.sub.textContent = m?.used != null ? `${fmtBytes(m.used)} used` : "";
    mem.viz.replaceChildren(sparkline(s.history.mem, "sv-spark--mem"));
    paintSessions();

    const d = s.disk;
    const dpct = d?.total ? ((d.used ?? 0) / d.total) * 100 : null;
    disk.value.textContent = d?.free != null ? fmtBytes(d.free) : "—";
    disk.value.dataset.level = level(dpct);
    disk.meta.textContent = d?.path ?? "/";
    disk.sub.textContent = d?.total ? `free of ${fmtBytes(d.total)}` : "";
    const ringWrap = h("div", "sp-ring");
    ringWrap.append(ring(dpct), h("span", "sp-ring__pct", fmtPct(dpct)));
    disk.viz.replaceChildren(ringWrap);
    el.title = s.at ? `updated ${new Date(s.at * 1000).toLocaleTimeString()}` : "";
  };

  const sessionsLine = h("div", "sp-card__foot");
  mem.el.append(sessionsLine);
  const paintSessions = (): void => {
    sessionsLine.replaceChildren();
    if (sessionsMem === null || !last?.mem?.total) return;
    const pct = (sessionsMem / last.mem.total) * 100;
    sessionsLine.append(
      h("span", "sp-card__foot-k", `${sessionsCount} session${sessionsCount === 1 ? "" : "s"}`),
      bar(pct, "sv-bar--thin"),
      h("span", "sp-card__foot-v", fmtBytes(sessionsMem)),
    );
    sessionsLine.title = `ccpipe's sessions use ${fmtBytes(sessionsMem)} (${fmtPct(pct)} of memory)`;
  };

  let prefs = loadStatsPrefs();
  el.hidden = !prefs.landing;
  const poller = new SysPoller(null, prefs.interval, paint);
  if (prefs.landing) poller.start();
  let started = prefs.landing;
  const unsub = onStatsPrefs((p) => {
    prefs = p;
    el.hidden = !p.landing;
    poller.setInterval(p.interval);
    if (p.landing && !started) { started = true; poller.start(); }
  });

  return {
    el,
    setSessionsMem(bytes, count) {
      sessionsMem = bytes;
      sessionsCount = count;
      paintSessions();
    },
    dispose() {
      poller.stop();
      unsub();
    },
  };
}
