// A drop-down panel under a button: the system status and the ping in a
// session's top bar, and the ping on the landing page. A tap or click on
// the button opens it and another closes it, as do a tap elsewhere and
// Escape. The button keeps the terminal's focus, and the phone's keyboard,
// where they were.

import { h } from "./dom";

export interface Dropdown {
  /** The open panel, or null. */
  readonly pop: HTMLElement | null;
  close(): void;
}

export interface DropdownOptions {
  /** The panel's accessible name. */
  label: string;
  /** Its classes (styles.css .sysmeter-pop is the look they share). */
  cls?: string;
  /** Widest it gets, in px; narrower on a phone. */
  width?: number;
  /** The panel is in the page and positioned: fill it. */
  onOpen(pop: HTMLElement): void;
  onClose?(): void;
}

export function dropdown(trigger: HTMLElement, opts: DropdownOptions): Dropdown {
  trigger.setAttribute("aria-haspopup", "dialog");
  trigger.setAttribute("aria-expanded", "false");
  // On a touch screen it's touchstart's default that moves focus (and hides
  // the keyboard); mousedown alone isn't enough (mobile.ts, the key strip).
  // Cancelling touchstart also cancels the click, so a touch opens the panel
  // on pointerup instead.
  trigger.addEventListener("mousedown", (e) => e.preventDefault());
  trigger.addEventListener("touchstart", (e) => e.preventDefault(), { passive: false });
  trigger.addEventListener("contextmenu", (e) => e.preventDefault());

  let pop: HTMLElement | null = null;

  const close = (): void => {
    if (!pop) return;
    pop.remove();
    pop = null;
    trigger.setAttribute("aria-expanded", "false");
    document.removeEventListener("pointerdown", onAway, true);
    document.removeEventListener("keydown", onKey, true);
    opts.onClose?.();
  };
  function onAway(e: Event): void {
    const t = e.target as Node;
    if (pop && !pop.contains(t) && !trigger.contains(t)) close();
  }
  function onKey(e: KeyboardEvent): void {
    if (e.key === "Escape") close();
  }

  const open = (): void => {
    pop = h("div", opts.cls ?? "sysmeter-pop");
    pop.setAttribute("role", "dialog");
    pop.setAttribute("aria-label", opts.label);
    const r = trigger.getBoundingClientRect();
    pop.style.top = `${Math.round(r.bottom + 6)}px`;
    pop.style.maxHeight = `${Math.round(window.innerHeight - r.bottom - 14)}px`;
    const width = Math.min(opts.width ?? 340, window.innerWidth - 16);
    pop.style.width = `${width}px`;
    pop.style.left = `${Math.round(Math.max(8, Math.min(window.innerWidth - width - 8, r.left + r.width / 2 - width / 2)))}px`;
    document.body.append(pop);
    trigger.setAttribute("aria-expanded", "true");
    document.addEventListener("pointerdown", onAway, true);
    document.addEventListener("keydown", onKey, true);
    opts.onOpen(pop);
  };
  const toggle = (): void => { if (pop) close(); else open(); };

  let touchedAt = 0;
  trigger.addEventListener("pointerup", (e) => {
    if (e.pointerType !== "touch") return;
    touchedAt = Date.now();
    toggle();
  });
  trigger.addEventListener("click", (e) => {
    e.stopPropagation();
    if (Date.now() - touchedAt < 700) return;      // already handled on pointerup
    toggle();
  });

  return {
    get pop() { return pop; },
    close,
  };
}
