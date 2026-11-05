// A drop-down panel under a button: the system status and the ping in a
// session's top bar, and the ping on the landing page. A tap or click on
// the button opens it and another closes it, as do a tap elsewhere,
// Escape, and scrolling the page it's on. In the top bar the button keeps
// the terminal's focus, and the phone's keyboard, where they were.

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
  /** Leave the terminal's focus (and the phone's keyboard) alone when the
   * button is pressed. Default true; the landing page has no terminal, and
   * there a swipe that starts on the button has to scroll the page. */
  keepFocus?: boolean;
  /** The panel is in the page and positioned: fill it. */
  onOpen(pop: HTMLElement): void;
  onClose?(): void;
}

export function dropdown(trigger: HTMLElement, opts: DropdownOptions): Dropdown {
  trigger.setAttribute("aria-haspopup", "dialog");
  trigger.setAttribute("aria-expanded", "false");
  if (opts.keepFocus ?? true) {
    // On a touch screen it's touchstart's default that moves focus (and
    // hides the keyboard); mousedown alone isn't enough (mobile.ts, the key
    // strip). Cancelling touchstart also cancels the click, so a touch or a
    // stylus opens the panel on pointerup instead.
    trigger.addEventListener("mousedown", (e) => e.preventDefault());
    trigger.addEventListener("touchstart", (e) => e.preventDefault(), { passive: false });
    trigger.addEventListener("contextmenu", (e) => e.preventDefault());
  }

  let pop: HTMLElement | null = null;

  const close = (): void => {
    if (!pop) return;
    pop.remove();
    pop = null;
    trigger.setAttribute("aria-expanded", "false");
    document.removeEventListener("pointerdown", onAway, true);
    document.removeEventListener("keydown", onKey, true);
    document.removeEventListener("scroll", onScroll, true);
    opts.onClose?.();
  };
  function onAway(e: Event): void {
    const t = e.target as Node;
    if (pop && !pop.contains(t) && !trigger.contains(t)) close();
  }
  function onKey(e: KeyboardEvent): void {
    if (e.key !== "Escape") return;
    // This Escape is the panel's. The terminal still has the focus, and
    // there it would reach Claude, whose Esc interrupts what it's doing.
    e.preventDefault();
    e.stopPropagation();
    close();
  }
  // The panel is placed once, under the button: scrolling what the button
  // is in would leave it floating. (Other scrolling, like the terminal's
  // as output arrives, doesn't move the button.)
  function onScroll(e: Event): void {
    const t = e.target;
    if (t === document || (t instanceof Node && t.contains(trigger))) close();
  }

  const open = (): void => {
    pop = h("div", opts.cls ?? "sysmeter-pop");
    pop.setAttribute("role", "dialog");
    pop.setAttribute("aria-label", opts.label);
    const r = trigger.getBoundingClientRect();
    pop.style.top = `${Math.round(r.bottom + 6)}px`;
    pop.style.maxHeight = `${Math.round(Math.max(120, window.innerHeight - r.bottom - 14))}px`;
    const width = Math.min(opts.width ?? 340, window.innerWidth - 16);
    pop.style.width = `${width}px`;
    pop.style.left = `${Math.round(Math.max(8, Math.min(window.innerWidth - width - 8, r.left + r.width / 2 - width / 2)))}px`;
    document.body.append(pop);
    trigger.setAttribute("aria-expanded", "true");
    document.addEventListener("pointerdown", onAway, true);
    document.addEventListener("keydown", onKey, true);
    document.addEventListener("scroll", onScroll, { capture: true, passive: true });
    opts.onOpen(pop);
  };
  const toggle = (): void => { if (pop) close(); else open(); };

  let touchedAt = 0;
  // Touch and stylus open it on pointerup (their click may be cancelled
  // above); a mouse uses the click. A swipe ends in pointercancel instead.
  trigger.addEventListener("pointerup", (e) => {
    if (e.pointerType === "mouse") return;
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
