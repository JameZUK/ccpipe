// Tooltips for the icon buttons (statusbar, tmux row, tile headers).
//
// A native `title` tooltip only appears on mouse hover, so on the iPad and
// the phone the icons had no explanation at all. This shows the button's
// title in a small styled bubble: after a short hover with a mouse/pen, or
// on press-and-hold with touch (the hold then doesn't also click).
//
// The title moves to data-tip the first time a button is hovered/pressed so
// the browser's own tooltip doesn't double up; aria-label stays for screen
// readers (set it, or it's copied from the title).

const SCOPE = ".statusbar, .tmuxbar, .tile__head";
const HOVER_DELAY_MS = 350;
const HOLD_DELAY_MS = 450;
const TOUCH_SHOW_MS = 1600;
const MOVE_CANCEL_PX = 10;

let bubble: HTMLDivElement | null = null;
let showTimer: number | null = null;
let hideTimer: number | null = null;
let current: HTMLElement | null = null;
let swallowClick = false;
let pressX = 0;
let pressY = 0;
let installed = false;

function tipTarget(e: Event): HTMLElement | null {
  const el = (e.target as Element | null)?.closest?.<HTMLElement>("button, [role=button]");
  if (!el || !el.closest(SCOPE)) return null;
  const title = el.getAttribute("title");
  if (title) {
    el.dataset.tip = title;
    if (!el.hasAttribute("aria-label")) el.setAttribute("aria-label", title);
    el.removeAttribute("title");
  }
  return el.dataset.tip ? el : null;
}

function clearTimers(): void {
  if (showTimer !== null) { clearTimeout(showTimer); showTimer = null; }
  if (hideTimer !== null) { clearTimeout(hideTimer); hideTimer = null; }
}

function show(el: HTMLElement): void {
  if (!el.isConnected || !el.dataset.tip) return;
  if (!bubble) {
    bubble = document.createElement("div");
    bubble.className = "tooltip";
    bubble.setAttribute("role", "tooltip");
    document.body.append(bubble);
  }
  bubble.textContent = el.dataset.tip;
  bubble.hidden = false;
  const r = el.getBoundingClientRect();
  const b = bubble.getBoundingClientRect();
  const margin = 8;
  let left = r.left + r.width / 2 - b.width / 2;
  left = Math.max(margin, Math.min(window.innerWidth - b.width - margin, left));
  let top = r.bottom + 6;
  if (top + b.height > window.innerHeight - margin) top = r.top - b.height - 6;
  bubble.style.left = `${Math.round(left)}px`;
  bubble.style.top = `${Math.round(Math.max(margin, top))}px`;
  current = el;
}

export function hideTooltip(): void {
  clearTimers();
  if (bubble) bubble.hidden = true;
  current = null;
}

export function installTooltips(): void {
  if (installed) return;
  installed = true;

  document.addEventListener("pointerover", (e) => {
    if (e.pointerType === "touch") return;
    const el = tipTarget(e);
    if (!el || el === current) return;
    hideTooltip();
    showTimer = window.setTimeout(() => show(el), HOVER_DELAY_MS);
  });
  document.addEventListener("pointerout", (e) => {
    if (e.pointerType === "touch") return;
    const el = (e.target as Element | null)?.closest?.("button, [role=button]");
    const to = (e.relatedTarget as Element | null)?.closest?.("button, [role=button]");
    if (el && el !== to) hideTooltip();
  });

  document.addEventListener("pointerdown", (e) => {
    swallowClick = false;
    if (e.pointerType !== "touch") { hideTooltip(); return; }
    const el = tipTarget(e);
    hideTooltip();
    if (!el) return;
    pressX = e.clientX;
    pressY = e.clientY;
    showTimer = window.setTimeout(() => {
      showTimer = null;
      show(el);
      swallowClick = true;        // a hold explains the button; it doesn't press it
      hideTimer = window.setTimeout(hideTooltip, TOUCH_SHOW_MS);
    }, HOLD_DELAY_MS);
  }, true);
  document.addEventListener("pointermove", (e) => {
    if (e.pointerType !== "touch" || showTimer === null) return;
    if (Math.abs(e.clientX - pressX) > MOVE_CANCEL_PX || Math.abs(e.clientY - pressY) > MOVE_CANCEL_PX) {
      clearTimers();
    }
  }, true);
  const endPress = (e: PointerEvent): void => {
    if (e.pointerType !== "touch") return;
    if (showTimer !== null) { clearTimeout(showTimer); showTimer = null; }
  };
  document.addEventListener("pointerup", endPress, true);
  document.addEventListener("pointercancel", endPress, true);
  document.addEventListener("click", (e) => {
    if (!swallowClick) return;
    swallowClick = false;
    e.preventDefault();
    e.stopPropagation();
  }, true);
  // A long press would otherwise open the OS callout / context menu.
  document.addEventListener("contextmenu", (e) => {
    if ((e.target as Element | null)?.closest?.(SCOPE)) e.preventDefault();
  });
  window.addEventListener("scroll", hideTooltip, true);
  window.addEventListener("blur", hideTooltip);
}
