// System status in a session's top bar, beside the ping. Wide screens show
// it inline (CPU graph + %, memory and disk bars, Claude plan usage, this
// project's size); on a phone it shrinks to a bar meter (CPU / memory /
// disk / plan). Either way a tap opens a drop-down with the graphs, the
// numbers, every plan meter with its reset time, and this session's own
// CPU, memory, project size and context fill. Off with Settings → Display.

import { h } from "./dom";
import {
  type PlanMeter, type PlanUsage, SysPoller, type StatsPrefs, type SysSnapshot, bar, fmtAgo, fmtBytes, fmtMoney,
  fmtPct, level, loadStatsPrefs, onStatsPrefs, paceBar, planHeadline, planLevel, planPace, planResets, refreshPlan,
  sparkline,
} from "./sysviz";

export interface SysMeter {
  el: HTMLElement;
  dispose(): void;
}


const diskPct = (s: SysSnapshot): number | null =>
  s.disk?.total ? ((s.disk.used ?? 0) / s.disk.total) * 100 : null;

const windowName = (m: PlanMeter): string =>
  m.group === "weekly" ? "the week" : m.kind === "session" ? "the 5 hours" : "the window";

export function mountSysMeter(session: string): SysMeter {
  const el = h("button", "sysmeter");
  el.type = "button";
  el.title = "System status";
  el.setAttribute("aria-label", "System status");
  el.setAttribute("aria-haspopup", "dialog");
  el.setAttribute("aria-expanded", "false");
  // Keep the terminal's focus (and the soft keyboard) where it was. On a
  // touch screen it's touchstart's default that moves focus (and hides the
  // keyboard) — mousedown alone isn't enough (mobile.ts, the key strip).
  // Cancelling touchstart also cancels the click, so a touch opens the
  // pop-up on pointerup instead.
  el.addEventListener("mousedown", (e) => e.preventDefault());
  el.addEventListener("touchstart", (e) => e.preventDefault(), { passive: false });
  el.addEventListener("contextmenu", (e) => e.preventDefault());

  // Inline (wide) readout.
  const inline = h("span", "sysmeter__inline");
  const cpuSpark = h("span", "sysmeter__spark");
  const cpuVal = h("span", "sysmeter__v");
  const memBar = h("span", "sysmeter__barwrap");
  const memVal = h("span", "sysmeter__v");
  const diskBar = h("span", "sysmeter__barwrap");
  const diskVal = h("span", "sysmeter__v");
  const planK = h("span", "sysmeter__k sysmeter__plan", "plan");
  const planBar = h("span", "sysmeter__barwrap sysmeter__plan");
  const planVal = h("span", "sysmeter__v sysmeter__plan");
  const projVal = h("span", "sysmeter__v sysmeter__proj");
  inline.append(
    h("span", "sysmeter__k", "cpu"), cpuSpark, cpuVal,
    h("span", "sysmeter__k", "mem"), memBar, memVal,
    h("span", "sysmeter__k", "disk"), diskBar, diskVal,
    planK, planBar, planVal,
    projVal,
  );
  // Compact (phone) meter: a bar each for CPU, memory, disk and the plan.
  const compact = h("span", "sysmeter__compact");
  const bars = [0, 1, 2, 3].map(() => {
    const b = h("span", "sysmeter__col");
    b.append(h("span", "sysmeter__col-fill"));
    compact.append(b);
    return b;
  });
  bars[3].classList.add("sysmeter__col--plan");
  el.append(inline, compact);

  let last: SysSnapshot | null = null;
  let pop: HTMLElement | null = null;
  let prefs = loadStatsPrefs();
  // The server's clock when the plan figures arrived, against ours: reset
  // countdowns run on the server's time.
  let planClock = { server: 0, local: 0 };
  const planNow = (): number => planClock.server + (Date.now() - planClock.local) / 1000;
  const plan = (): PlanUsage | null => (prefs.plan ? last?.plan ?? null : null);

  const paintPlan = (): void => {
    const p = plan();
    const m = planHeadline(p);
    const shown = !!m;
    for (const node of [planK, planBar, planVal]) node.hidden = !shown;
    bars[3].hidden = !shown;
    if (!m) return;
    const lvl = planLevel(m);
    planBar.replaceChildren(paceBar(m, planNow()));
    planVal.textContent = fmtPct(m.percent);
    planVal.dataset.level = lvl;
    const fill = bars[3].firstElementChild as HTMLElement;
    fill.style.height = `${Math.max(8, Math.min(100, m.percent))}%`;
    bars[3].dataset.level = lvl;
  };

  const paint = (s: SysSnapshot): void => {
    last = s;
    if (s.plan) planClock = { server: s.plan.now, local: Date.now() };
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
    paintPlan();
    const head = planHeadline(plan());
    el.title = `cpu ${fmtPct(c)} · memory ${fmtPct(m)} · disk ${fmtPct(d)}`
      + (head ? ` · plan ${head.label} ${fmtPct(head.percent)}` : "")
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

  // A key for the bar meter (only when that's what the top bar shows — the
  // wide readout labels itself): the bars as they are now, named, and what
  // their colours mean.
  const meterKey = (): HTMLElement => {
    const k = h("div", "sysmeter-key");
    const cols = h("div", "sysmeter-key__cols");
    ["cpu", "mem", "disk", "plan"].forEach((name, i) => {
      if (bars[i].hidden) return;
      const col = h("span", "sysmeter__col");
      if (i === 3) col.classList.add("sysmeter__col--plan");
      col.dataset.level = bars[i].dataset.level ?? "ok";
      const fill = h("span", "sysmeter__col-fill");
      fill.style.height = (bars[i].firstElementChild as HTMLElement).style.height;
      col.append(fill);
      const item = h("span", "sysmeter-key__item");
      item.append(col, h("span", "sysmeter-key__name", name));
      cols.append(item);
    });
    const levels = h("div", "sysmeter-key__levels");
    for (const [lvl, text] of [["ok", "under 70%"], ["warn", "70%+"], ["hot", "90%+"]]) {
      const item = h("span", "sysmeter-key__level");
      const sw = h("span", "sysmeter-key__swatch");
      sw.dataset.level = lvl;
      item.append(sw, text);
      levels.append(item);
    }
    k.append(cols, levels);
    return k;
  };

  // Every meter the plan reports, with its reset time and how much of its
  // window has gone (the tick on its bar); the usage credits; how old the
  // figures are.
  const planSection = (p: PlanUsage): HTMLElement => {
    const sec = h("div", "sysmeter-pop__plan");
    sec.append(h("div", "sysmeter-pop__title", "claude plan"));
    const now = planNow();
    for (const m of p.meters) {
      const r = h("div", "sysmeter-pop__row sysmeter-pop__row--plan");
      const head = h("div", "sysmeter-pop__head");
      const v = h("span", "sysmeter-pop__v", fmtPct(m.percent));
      v.dataset.level = planLevel(m);
      head.append(h("span", "sysmeter-pop__k", m.label), v);
      const pace = planPace(m, now);
      r.append(head, paceBar(m, now, "sysmeter-pop__pbar"));
      const resets = planResets(m, now);
      if (resets) r.append(h("div", "sysmeter-pop__sub", resets));
      if (pace !== null) {
        r.append(h("div", "sysmeter-pop__sub sysmeter-pop__pace",
          `the tick: ${Math.round(pace)}% of ${windowName(m)} gone`));
      }
      r.title = m.source === "statusline" ? "from Claude's last reply" : "from /usage";
      sec.append(r);
    }
    const cr = p.credits;
    if (cr && (cr.enabled || cr.limit !== null || (cr.used ?? 0) > 0)) {
      const spend = `${fmtMoney(cr.used ?? 0, cr.currency)}${cr.limit !== null ? ` of ${fmtMoney(cr.limit, cr.currency)}` : ""} this month`;
      sec.append(h("div", "sysmeter-pop__sub sysmeter-pop__credits",
        `usage credits ${cr.enabled ? "on" : "off"} · ${spend}`));
    }
    const notes: string[] = [];
    if (p.at) notes.push(`as of ${fmtAgo(Math.max(0, now - p.at))}`);
    if (p.checking) notes.push("checking…");
    else if (p.error && !p.meters.length) notes.push(p.error);
    else if (p.error) notes.push("last check failed");
    if (!p.meters.length && !p.checking && !p.error) notes.push("no figures yet");
    const note = h("div", "sysmeter-pop__sub sysmeter-pop__asof", notes.join(" · "));
    if (p.error) note.title = p.error;
    sec.append(note);
    return sec;
  };

  const paintPop = (): void => {
    if (!pop || !last) return;
    const s = last;
    const d = diskPct(s);
    const ses = s.session;
    const p = plan();
    const kids: HTMLElement[] = [
      ...(compact.getClientRects().length ? [meterKey()] : []),
      row("cpu", fmtPct(s.cpu?.pct), sparkline(s.history.cpu),
        s.cpu ? `${s.cpu.cores} cores · load ${s.cpu.load.map((x) => x.toFixed(2)).join(" ")}` : ""),
      row("memory", fmtPct(s.mem?.pct), sparkline(s.history.mem, "sv-spark--mem"),
        s.mem?.total ? `${fmtBytes(s.mem.used)} of ${fmtBytes(s.mem.total)}`
          + (s.mem.swap_used ? ` · swap ${fmtBytes(s.mem.swap_used)}` : "") : ""),
      row(`disk ${s.disk?.path ?? "/"}`, fmtPct(d), bar(d),
        s.disk?.total ? `${fmtBytes(s.disk.free)} free of ${fmtBytes(s.disk.total)}` : ""),
      ...(p ? [planSection(p)] : []),
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
    const ctx = prefs.plan ? ses?.context : null;
    if (ctx) {
      const of = ctx.window ? ` of ${ctx.window >= 1e6 ? `${ctx.window / 1e6}M` : `${Math.round(ctx.window / 1000)}k`} tokens` : "";
      cell("context", fmtPct(ctx.percent),
        `context window${of}${ctx.model ? ` · ${ctx.model}` : ""} · after Claude's last reply`);
      grid.classList.add("sysmeter-pop__grid--4");
    }
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
  let touchedAt = 0;
  el.addEventListener("pointerup", (e) => {
    if (e.pointerType !== "touch") return;
    touchedAt = Date.now();
    toggle();
  });
  el.addEventListener("click", (e) => {
    e.stopPropagation();
    if (Date.now() - touchedAt < 700) return;      // already handled on pointerup
    toggle();
  });
  function toggle(): void {
    if (pop) { closePop(); return; }
    pop = h("div", "sysmeter-pop");
    pop.setAttribute("role", "dialog");
    pop.setAttribute("aria-label", "System status");
    const r = el.getBoundingClientRect();
    pop.style.top = `${Math.round(r.bottom + 6)}px`;
    pop.style.maxHeight = `${Math.round(window.innerHeight - r.bottom - 14)}px`;
    const width = Math.min(340, window.innerWidth - 16);
    pop.style.width = `${width}px`;
    pop.style.left = `${Math.round(Math.max(8, Math.min(window.innerWidth - width - 8, r.left + r.width / 2 - width / 2)))}px`;
    document.body.append(pop);
    el.setAttribute("aria-expanded", "true");
    paintPop();
    if (!last) pop.append(h("div", "sysmeter-pop__sub", "loading…"));
    document.addEventListener("pointerdown", onAway, true);
    document.addEventListener("keydown", onKey, true);
    // The plan figures may be a quarter of an hour old: ask for a fresh
    // check (the server only runs one when they're over five minutes old).
    if (prefs.plan && last?.plan) {
      void refreshPlan().then((res) => {
        if (!res?.plan || !last) return;
        last.plan = { ...res.plan, checking: res.plan.checking || res.checking };
        planClock = { server: res.plan.now, local: Date.now() };
        paintPlan();
        if (pop) paintPop();
      });
    }
  }

  const poller = new SysPoller(session, loadStatsPrefs().interval, paint);
  const apply = (p: StatsPrefs): void => {
    prefs = p;
    el.hidden = !p.sessionBar;
    if (!p.sessionBar) closePop();
    poller.setInterval(p.interval);
    poller.setActive(p.sessionBar);
    if (last) paint(last);
  };
  apply(prefs);
  const unsub = onStatsPrefs(apply);

  return {
    el,
    dispose() {
      poller.stop();
      unsub();
      closePop();
    },
  };
}
