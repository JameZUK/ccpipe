// The tmux toolbar: a slim row under the statusbar that drives tmux for
// the focused session (the main terminal's, or a focused tile's) —
// windows, pane splits, copy-mode, and a raw prefix key.
//
// Every button names a fixed server-side action (POST /api/sessions/<s>/tmux,
// see backend tmux_ui.py) — nothing typed here reaches tmux's command
// parser. Only the prefix key goes down the terminal socket as a keystroke.

import { listWindows, tmuxAction, type TmuxAction, type TmuxWindow } from "./api";
import {
  ICON_COPY, ICON_EXPAND, ICON_NEXT, ICON_PENCIL, ICON_PLUS, ICON_SPLIT_H, ICON_SPLIT_V, ICON_X,
} from "./icons";
import { inlineConfirm, inlinePrompt } from "./inline-prompt";
import type { TerminalSocket } from "./ws";

export interface TmuxTarget {
  session: string;
  /** Socket for the prefix key; null when the target can't take input. */
  socket: TerminalSocket | null;
}

export interface TmuxBar {
  el: HTMLElement;
  /** Re-read the target's windows (focus moved, or tmux reported a change). */
  refresh(): void;
  dispose(): void;
}

export function mountTmuxBar(getTarget: () => TmuxTarget): TmuxBar {
  const el = document.createElement("div");
  el.className = "tmuxbar";
  el.setAttribute("role", "toolbar");
  el.setAttribute("aria-label", "tmux");

  const target = document.createElement("span");
  target.className = "tmuxbar__target";
  const tabs = document.createElement("div");
  tabs.className = "tmuxbar__tabs";
  el.append(target, tabs);

  const status = document.createElement("span");
  status.className = "tmuxbar__status";
  status.hidden = true;
  let statusTimer: number | null = null;
  const flash = (msg: string): void => {
    status.textContent = msg;
    status.hidden = false;
    if (statusTimer !== null) clearTimeout(statusTimer);
    statusTimer = window.setTimeout(() => { status.hidden = true; statusTimer = null; }, 2500);
  };

  let windows: TmuxWindow[] = [];
  let seq = 0;

  const run = async (action: TmuxAction, arg?: number | string): Promise<void> => {
    const { session } = getTarget();
    try {
      windows = await tmuxAction(session, action, arg);
      renderTabs();
    } catch (e) {
      flash(e instanceof Error ? e.message : "tmux action failed");
    }
  };

  const button = (label: string, title: string, onClick: () => void, cls = ""): HTMLButtonElement => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = `tmuxbar__btn ${cls}`.trim();
    // Icons are our own constant SVG strings; text labels go in as text.
    if (label.startsWith("<svg")) { b.innerHTML = label; b.classList.add("tmuxbar__btn--icon"); }
    else b.textContent = label;
    b.title = title;
    b.setAttribute("aria-label", title);
    // Keep focus (and the soft keyboard) where it was.
    b.addEventListener("mousedown", (e) => e.preventDefault());
    b.addEventListener("click", onClick);
    return b;
  };
  const group = (...items: HTMLElement[]): HTMLElement => {
    const g = document.createElement("div");
    g.className = "tmuxbar__group";
    g.append(...items);
    return g;
  };

  const renderTabs = (): void => {
    tabs.replaceChildren();
    for (const w of windows) {
      const b = button(`${w.index}:${w.name}`, `Window ${w.index}${w.panes > 1 ? ` · ${w.panes} panes` : ""}${w.zoomed ? " · zoomed" : ""}`,
        () => { if (!w.active) void run("window-select", w.index); },
        "tmuxbar__tab");
      b.classList.toggle("active", w.active);
      if (w.panes > 1) b.dataset.panes = String(w.panes);
      tabs.append(b);
    }
    const active = windows.find((w) => w.active);
    zoomBtn.classList.toggle("active", !!active?.zoomed);
  };

  const zoomBtn = button(ICON_EXPAND, "Zoom the active pane to the whole window (again to unzoom)", () => void run("pane-zoom"));

  el.append(
    group(
      button(ICON_PLUS, "New window", () => void run("window-new")),
      button(ICON_PENCIL, "Rename window", async () => {
        const cur = windows.find((w) => w.active);
        const name = await inlinePrompt("Rename window", cur?.name ?? "");
        if (name && name.trim()) void run("window-rename", name.trim());
      }),
      button(ICON_X, "Close window", async () => {
        const cur = windows.find((w) => w.active);
        if (await inlineConfirm(`Close window ${cur ? `${cur.index}:${cur.name}` : ""} and everything running in it?`, "Close"))
          void run("window-close");
      }, "tmuxbar__danger"),
    ),
    group(
      button(ICON_SPLIT_H, "Split: new pane side by side", () => void run("split-h")),
      button(ICON_SPLIT_V, "Split: new pane below", () => void run("split-v")),
      button(ICON_NEXT, "Next pane", () => void run("pane-next")),
      zoomBtn,
      button(ICON_X, "Close pane", async () => {
        if (await inlineConfirm("Close the active pane and whatever is running in it?", "Close"))
          void run("pane-close");
      }, "tmuxbar__danger"),
    ),
    group(
      button(ICON_COPY, "Copy mode (scroll / select with tmux)", () => void run("copy-mode")),
      button("PgUp", "Page up (enters copy mode)", () => void run("page-up")),
      button("PgDn", "Page down", () => void run("page-down")),
      button("Esc", "Leave copy mode", () => void run("copy-exit")),
    ),
    group(
      button("C-b", "Send the tmux prefix key (Ctrl-B); then press a tmux key", () => {
        const { socket } = getTarget();
        if (socket) socket.send({ type: "input", data: "\x02" });
        else flash("this tile is watch-only");
      }, "tmuxbar__prefix"),
    ),
    status,
  );

  const refresh = (): void => {
    const { session } = getTarget();
    target.textContent = session;
    target.title = `tmux controls act on ${session}`;
    const mine = ++seq;
    listWindows(session).then((w) => {
      if (mine !== seq) return;          // a newer refresh superseded this one
      windows = w;
      renderTabs();
    }).catch(() => { /* session gone / offline: keep what we had */ });
  };

  return {
    el,
    refresh,
    dispose() {
      if (statusTimer !== null) clearTimeout(statusTimer);
      el.remove();
    },
  };
}
