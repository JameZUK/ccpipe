// The statusbar "tiles" menu: add another session to the screen (beside or
// below the focused tile), arrange the tiles with a preset, or clear them.
// Reuses the docs-menu look.

import type { Preset, TileLayout } from "./layout";

let open: { menu: HTMLElement; close: () => void } | null = null;

export function toggleTilesMenu(anchor: HTMLElement, layout: TileLayout): void {
  if (open) { open.close(); return; }
  const menu = document.createElement("div");
  menu.className = "docs-menu tiles-menu";
  const r = anchor.getBoundingClientRect();
  menu.style.top = `${Math.round(r.bottom + 6)}px`;
  menu.style.right = `${Math.round(window.innerWidth - r.right)}px`;

  const close = (): void => {
    menu.remove();
    document.removeEventListener("pointerdown", onAway, true);
    document.removeEventListener("keydown", onKey, true);
    open = null;
  };
  const onAway = (e: Event): void => {
    const t = e.target as Node;
    if (!menu.contains(t) && !anchor.contains(t)) close();
  };
  const onKey = (e: KeyboardEvent): void => { if (e.key === "Escape") close(); };
  document.addEventListener("pointerdown", onAway, true);
  document.addEventListener("keydown", onKey, true);
  open = { menu, close };

  const section = (label: string, divider: boolean): void => {
    if (divider) {
      const hr = document.createElement("div");
      hr.className = "docs-menu__divider";
      hr.setAttribute("role", "separator");
      menu.append(hr);
    }
    const h = document.createElement("div");
    h.className = "docs-menu__section";
    h.textContent = label;
    menu.append(h);
  };
  const note = (text: string): void => {
    const n = document.createElement("div");
    n.className = "docs-menu__note";
    n.textContent = text;
    menu.append(n);
  };
  const action = (label: string, title: string, fn: () => void): HTMLButtonElement => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "tiles-menu__btn";
    b.textContent = label;
    b.title = title;
    b.addEventListener("click", () => { close(); fn(); });
    return b;
  };

  section("Add a session", false);
  const list = document.createElement("div");
  list.className = "tiles-menu__list";
  list.append(Object.assign(document.createElement("div"), {
    className: "docs-menu__note", textContent: "Loading…",
  }));
  menu.append(list);

  const tiled = layout.tiledSessions();
  if (tiled.length) {
    section("Arrange", true);
    const presets: Array<[Preset, string, string]> = [
      ["side", "Side by side", "All tiles in one row"],
      ["grid", "Grid", "Tiles in a grid"],
      ["main-stack", "Main + stack", "Main terminal on the left, the others stacked on the right"],
      ["stack", "Stacked", "All tiles one above the other"],
    ];
    const row = document.createElement("div");
    row.className = "tiles-menu__row";
    for (const [kind, label, title] of presets) row.append(action(label, title, () => layout.preset(kind)));
    menu.append(row);
    const clear = action("Close all tiles", "Back to just the main terminal (the sessions keep running)",
                         () => layout.clear());
    clear.classList.add("tiles-menu__clear");
    menu.append(clear);
  }
  if (!layout.capable()) note("The window is too narrow to show tiles.");

  document.body.append(menu);

  void fetch("/api/sessions", { credentials: "same-origin" })
    .then((res) => (res.ok ? res.json() : []))
    .then((sessions: Array<{ name?: string }>) => {
      if (open?.menu !== menu) return;
      list.replaceChildren();
      const taken = new Set(tiled);
      const names = (Array.isArray(sessions) ? sessions : [])
        .map((s) => s?.name)
        .filter((n): n is string => typeof n === "string" && !n.startsWith("__ccpipe"))
        .filter((n) => !taken.has(n) && !layout.isOwner(n));
      if (!names.length) {
        list.append(Object.assign(document.createElement("div"), {
          className: "docs-menu__note", textContent: "No other sessions to add.",
        }));
        return;
      }
      if (layout.tileCount >= 9) {
        list.append(Object.assign(document.createElement("div"), {
          className: "docs-menu__note", textContent: "At most 9 tiles.",
        }));
        return;
      }
      for (const name of names) {
        const item = document.createElement("div");
        item.className = "tiles-menu__item";
        const label = document.createElement("span");
        label.className = "tiles-menu__name";
        label.textContent = name;
        item.append(label,
          action("→ beside", `Show ${name} beside the focused tile`, () => layout.add(name, "h")),
          action("↓ below", `Show ${name} below the focused tile`, () => layout.add(name, "v")));
        list.append(item);
      }
    })
    .catch(() => {
      if (open?.menu === menu) list.replaceChildren(Object.assign(document.createElement("div"), {
        className: "docs-menu__note", textContent: "Couldn't load sessions.",
      }));
    });
}

export function closeTilesMenu(): void {
  open?.close();
}
