// Settings sections for the session lifecycle and the status graphs.
//
// - "status & graphs" (Display tab): per device (localStorage, sysviz.ts) —
//   the landing-page system panel, the session-bar status, per-session RAM
//   and disk, and how often they refresh. Applied immediately.
// - "sessions" (Sessions tab): server-side (/api/session-config,
//   lifecycle.py) — how many of the most recent sticky / other sessions
//   start at boot, and after how long completely idle each kind is stopped.

import { apiJson } from "./api";
import { deviceClass } from "./mobile";
import { STATS_INTERVALS, loadStatsPrefs, saveStatsPrefs, type StatsPrefs } from "./sysviz";

interface SessionsConfig {
  boot_sticky: number;
  boot_other: number;
  idle_stop_sticky_min: number;
  idle_stop_other_min: number;
}

function h<K extends keyof HTMLElementTagNameMap>(tag: K, cls?: string, text?: string): HTMLElementTagNameMap[K] {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  if (text !== undefined) el.textContent = text;
  return el;
}

function section(title: string): { sec: HTMLElement; rows: HTMLElement } {
  const sec = h("section", "modal__section");
  sec.append(h("h3", "modal__section-title", title));
  const rows = h("div", "modal__rows");
  sec.append(rows);
  return { sec, rows };
}

function row(label: string, hint: string, control: HTMLElement): HTMLLabelElement {
  const r = h("label", "row");
  const l = h("span", "row__label", label);
  if (hint) l.append(h("span", "row__hint", hint));
  r.append(l, control);
  return r;
}

function checkbox(checked: boolean): HTMLInputElement {
  const c = h("input", "checkbox");
  c.type = "checkbox";
  c.checked = checked;
  return c;
}

function select(options: Array<[string, string]>, value: string): HTMLSelectElement {
  const s = h("select", "select");
  for (const [v, label] of options) {
    const o = h("option", undefined, label);
    o.value = v;
    s.append(o);
  }
  if (!options.some(([v]) => v === value)) {
    const o = h("option", undefined, value);
    o.value = value;
    s.append(o);
  }
  s.value = value;
  return s;
}

const fmtMinutes = (m: number): string => {
  if (m <= 0) return "never";
  if (m < 60) return `${m} min`;
  if (m < 24 * 60) return `${m / 60} h`.replace(".5 h", "½ h");
  return `${m / (24 * 60)} day${m === 1440 ? "" : "s"}`;
};

export function buildStatsSection(): HTMLElement {
  const { sec, rows } = section("status & graphs");
  const p = loadStatsPrefs();
  const landing = checkbox(p.landing);
  const bar = checkbox(p.sessionBar);
  const per = checkbox(p.perSession);
  const every = select(STATS_INTERVALS.map((s) => [String(s), `${s} s`]), String(p.interval));
  const only = `this ${deviceClass()} only`;
  rows.append(
    row("System panel on the landing page", `${only} · CPU, memory and disk graphs`, landing),
    row("Status in a session's top bar", `${only} · beside the ping; tap it for details`, bar),
    row("Memory & disk per session", `${only} · on the landing page`, per),
    row("Refresh every", "", every),
  );
  const save = (): void => {
    const next: StatsPrefs = {
      landing: landing.checked, sessionBar: bar.checked, perSession: per.checked,
      interval: parseInt(every.value, 10) || 5,
    };
    saveStatsPrefs(next);
  };
  for (const c of [landing, bar, per, every]) c.addEventListener("change", save);
  return sec;
}

export function buildSessionsSection(): HTMLElement {
  const { sec, rows } = section("sessions");
  const intro = h("p", "modal__note",
    "Every session is remembered. A stopped one is listed on the landing page and resumes "
    + "— same directory, same Claude conversation — when you open it.");
  rows.before(intro);

  const num = (): HTMLInputElement => {
    const i = h("input", "input");
    i.type = "number";
    i.min = "0";
    i.max = "32";
    i.step = "1";
    i.inputMode = "numeric";
    return i;
  };
  const bootSticky = num();
  const bootOther = num();
  const stickyIdle = select([["0", "never"], ["60", "1 h"], ["120", "2 h"], ["240", "4 h"], ["480", "8 h"],
    ["720", "12 h"], ["1440", "1 day"], ["2880", "2 days"], ["10080", "7 days"]], "1440");
  const otherIdle = select([["0", "never"], ["15", "15 min"], ["30", "30 min"], ["60", "1 h"], ["120", "2 h"],
    ["240", "4 h"], ["480", "8 h"], ["1440", "1 day"]], "120");
  rows.append(
    row("Start at boot · sticky", "the most recently used sticky sessions; the rest resume when opened", bootSticky),
    row("Start at boot · other", "the most recently used non-sticky sessions", bootOther),
    row("Stop sticky sessions idle for", "", stickyIdle),
    row("Stop other sessions idle for", "", otherIdle),
  );
  sec.append(h("p", "modal__note",
    "Idle means completely idle: no terminal attached, no output or input, Claude not working, "
    + "no command or background task running, no CPU use, and no question waiting for you. "
    + "Anything running keeps the session running."));

  const actions = h("div", "modal__row-actions");
  const status = h("span", "modal__status");
  const saveBtn = h("button", "btn btn--primary", "Save sessions");
  saveBtn.type = "button";
  actions.append(status, saveBtn);
  sec.append(actions);

  const setStatus = (msg: string, error = false): void => {
    status.textContent = msg;
    status.classList.toggle("modal__status--error", error);
  };
  const fill = (cfg: SessionsConfig): void => {
    bootSticky.value = String(cfg.boot_sticky);
    bootOther.value = String(cfg.boot_other);
    const setSel = (s: HTMLSelectElement, minutes: number): void => {
      const v = String(minutes);
      if (![...s.options].some((o) => o.value === v)) {
        const o = h("option", undefined, fmtMinutes(minutes));
        o.value = v;
        s.append(o);
      }
      s.value = v;
    };
    setSel(stickyIdle, cfg.idle_stop_sticky_min);
    setSel(otherIdle, cfg.idle_stop_other_min);
  };
  void apiJson<SessionsConfig>("/api/session-config").then(fill)
    .catch((err) => setStatus(`failed to load: ${(err as Error).message}`, true));

  saveBtn.addEventListener("click", async () => {
    const clamp = (v: string): number => Math.max(0, Math.min(32, parseInt(v, 10) || 0));
    setStatus("saving…");
    saveBtn.disabled = true;
    try {
      const next = await apiJson<SessionsConfig>("/api/session-config", {
        method: "POST",
        body: JSON.stringify({
          boot_sticky: clamp(bootSticky.value),
          boot_other: clamp(bootOther.value),
          idle_stop_sticky_min: parseInt(stickyIdle.value, 10) || 0,
          idle_stop_other_min: parseInt(otherIdle.value, 10) || 0,
        }),
      });
      fill(next);
      setStatus("saved");
      setTimeout(() => setStatus(""), 1500);
    } catch (err) {
      setStatus(`save failed: ${(err as Error).message}`, true);
    } finally {
      saveBtn.disabled = false;
    }
  });
  return sec;
}
