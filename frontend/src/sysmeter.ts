// System status in a session's top bar, beside the ping. Wide screens show
// it inline (CPU graph + %, memory and disk bars, this project's size); on a
// phone it shrinks to a three-bar meter (CPU / memory / disk). Either way a
// tap opens a drop-down with the graphs, the numbers, and this session's
// own CPU, memory and project size. Off with Settings → Display.

import {
  SysPoller, type SysSnapshot, bar, fmtBytes, fmtPct, level, loadStatsPrefs, onStatsPrefs, sparkline,
} from "./sysviz";

export interface SysMeter {
  el: HTMLElement;
  dispose(): void;
}

function h<K extends keyof HTMLElementTagNameMap>(tag: K, cls?: string, text?: string): HTMLElementTagNameMap[K] {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  if (text !== undefined) el.textContent = text;
  return el;
}

const diskPct = (s: SysSnapshot): number | null =>
  s.disk?.total ? ((s.disk.used ?? 0) / s.disk.total) * 100 : null;

export function mountSysMeter(session: string): SysMeter {
  const el = h("button", "sysmeter");
  el.type = "button";
  el.title = "System status";
  el.setAttribute("aria-label", "System status");
  el.setAttribute("aria-haspopup", "dialog");
  el.setAttribute("aria-expanded", "false");
  // Keep the terminal's focus (and the soft keyboard) where it was.
  el.addEventListener("mousedown", (e) => e.preventDefault());

  // Inline (wide) readout.
  const inline = h("span", "sysmeter__inline");
  const cpuSpark = h("span", "sysmeter__spark");
  const cpuVal = h("span", "sysmeter__v");
  const memBar = h("span", "sysmeter__barwrap");
  const memVal = h("span", "sysmeter__v");
  const diskBar = h("span", "sysmeter__barwrap");
  const diskVal = h("span", "sysmeter__v");
  const projVal = h("span", "sysmeter__v sysmeter__proj");
  inline.append(
    h("span", "sysmeter__k", "cpu"), cpuSpark, cpuVal,
    h("span", "sysmeter__k", "mem"), memBar, memVal,
    h("span", "sysmeter__k", "disk"), diskBar, diskVal,
    projVal,
  );
  // Compact (phone) meter: three bars.
  const compact = h("span", "sysmeter__compact");
  const bars = [0, 1, 2].map(() => {
    const b = h("span", "sysmeter__col");
    b.append(h("span", "sysmeter__col-fill"));
    compact.append(b);
    return b;
  });
  el.append(inline, compact);

  let last: SysSnapshot | null = null;
  let pop: HTMLElement | null = null;

  const paint = (s: SysSnapshot): void => {
    last = s;
    const c = s.cpu?.pct ?? null;
    const m = s.mem?.pct ?? null;
    const d = diskPct(s);
    cpuSpark.replaceChildren(sparkline(s.history.cpu, "", 24));
    cpuVal.textContent = fmtPct(c);
    memBar.replaceChildren(bar(m));
    memVal.textContent = fmtPct(m);
    diskBar.replaceChildren(bar(d));
    diskVal.textContent = fmtPct(d);
    const proj = s.session?.disk;
    projVal.textContent = proj != null ? fmtBytes(proj) : "";
    projVal.title = proj != null ? `this project: ${fmtBytes(proj)}` : "";
    [c, m, d].forEach((v, i) => {
      const fill = bars[i].firstElementChild as HTMLElement;
      fill.style.height = `${Math.max(8, Math.min(100, v ?? 0))}%`;
      bars[i].dataset.level = level(v);
    });
    el.title = `cpu ${fmtPct(c)} · memory ${fmtPct(m)} · disk ${fmtPct(d)}`
      + (proj != null ? ` · project ${fmtBytes(proj)}` : "");
    if (pop) paintPop();
  };

  const row = (label: string, value: string, viz: Element | null, sub: string): HTMLElement => {
    const r = h("div", "sysmeter-pop__row");
    const head = h("div", "sysmeter-pop__head");
    head.append(h("span", "sysmeter-pop__k", label), h("span", "sysmeter-pop__v", value));
    r.append(head);
    if (viz) {
      const v = h("div", "sysmeter-pop__viz");
      v.append(viz);
      r.append(v);
    }
    if (sub) r.append(h("div", "sysmeter-pop__sub", sub));
    return r;
  };

  const paintPop = (): void => {
    if (!pop || !last) return;
    const s = last;
    const d = diskPct(s);
    const ses = s.session;
    const kids: HTMLElement[] = [
      row("cpu", fmtPct(s.cpu?.pct), sparkline(s.history.cpu),
        s.cpu ? `${s.cpu.cores} cores · load ${s.cpu.load.map((x) => x.toFixed(2)).join(" ")}` : ""),
      row("memory", fmtPct(s.mem?.pct), sparkline(s.history.mem, "sv-spark--mem"),
        s.mem?.total ? `${fmtBytes(s.mem.used)} of ${fmtBytes(s.mem.total)}`
          + (s.mem.swap_used ? ` · swap ${fmtBytes(s.mem.swap_used)}` : "") : ""),
      row(`disk ${s.disk?.path ?? "/"}`, fmtPct(d), bar(d),
        s.disk?.total ? `${fmtBytes(s.disk.free)} free of ${fmtBytes(s.disk.total)}` : ""),
    ];
    const mine = h("div", "sysmeter-pop__mine");
    mine.append(h("div", "sysmeter-pop__title", `this session · ${session}`));
    const grid = h("div", "sysmeter-pop__grid");
    const cell = (k: string, v: string, title = ""): void => {
      const c = h("div", "sysmeter-pop__cell");
      c.append(h("span", "sysmeter-pop__k", k), h("span", "sysmeter-pop__v", v));
      if (title) c.title = title;
      grid.append(c);
    };
    cell("memory", fmtBytes(ses?.mem), "its processes: Claude, MCP helpers, anything they started");
    cell("cpu", ses?.cpu != null ? `${ses.cpu.toFixed(1)}%` : "—", "share of the whole machine");
    cell("project", fmtBytes(ses?.disk), ses?.cwd ?? "");
    mine.append(grid);
    if (ses?.cwd) mine.append(h("div", "sysmeter-pop__sub sysmeter-pop__cwd", ses.cwd));
    pop.replaceChildren(...kids, mine);
  };

  const closePop = (): void => {
    pop?.remove();
    pop = null;
    el.setAttribute("aria-expanded", "false");
    document.removeEventListener("pointerdown", onAway, true);
    document.removeEventListener("keydown", onKey, true);
  };
  function onAway(e: Event): void {
    const t = e.target as Node;
    if (pop && !pop.contains(t) && !el.contains(t)) closePop();
  }
  function onKey(e: KeyboardEvent): void {
    if (e.key === "Escape") closePop();
  }
  el.addEventListener("click", (e) => {
    e.stopPropagation();
    if (pop) { closePop(); return; }
    pop = h("div", "sysmeter-pop");
    pop.setAttribute("role", "dialog");
    pop.setAttribute("aria-label", "System status");
    const r = el.getBoundingClientRect();
    pop.style.top = `${Math.round(r.bottom + 6)}px`;
    const width = Math.min(340, window.innerWidth - 16);
    pop.style.width = `${width}px`;
    pop.style.left = `${Math.round(Math.max(8, Math.min(window.innerWidth - width - 8, r.left + r.width / 2 - width / 2)))}px`;
    document.body.append(pop);
    el.setAttribute("aria-expanded", "true");
    paintPop();
    if (!last) pop.append(h("div", "sysmeter-pop__sub", "loading…"));
    document.addEventListener("pointerdown", onAway, true);
    document.addEventListener("keydown", onKey, true);
  });

  let prefs = loadStatsPrefs();
  const poller = new SysPoller(session, prefs.interval, paint);
  let running = false;
  const apply = (): void => {
    el.hidden = !prefs.sessionBar;
    if (prefs.sessionBar && !running) { running = true; poller.start(); }
    if (!prefs.sessionBar) closePop();
    poller.setInterval(prefs.interval);
  };
  apply();
  const unsub = onStatsPrefs((p) => { prefs = p; apply(); });

  return {
    el,
    dispose() {
      poller.stop();
      unsub();
      closePop();
    },
  };
}
