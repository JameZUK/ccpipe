// Select text on a touch screen. The terminal is drawn on a canvas, a drag
// on it scrolls (or is sent to Claude as the mouse wheel), and xterm has no
// touch selection, so there was no good way to copy from it on a phone. A
// long-press on the terminal opens this view instead: the screen and its
// scrollback as plain text, where the phone's own selection works (handles,
// magnifier, Copy / Share). It opens at the line that was pressed with the
// word under the finger selected, and adds Copy, Copy all, and a list of
// the links in the text. ✕, Escape or the Android back button closes it.

import type { Terminal } from "@xterm/xterm";
import { h } from "./dom";

const MAX_LINES = 3000;
// Trailing punctuation is usually the sentence, not the link.
const URL_RE = /\bhttps?:\/\/[^\s<>"'`]+/g;
const URL_TRAIL_RE = /[).,;:!?\]'"]+$/;

export interface SelectViewOptions {
  term: Terminal;
  /** Where the long-press landed: buffer row (absolute) and column. */
  row: number;
  col: number;
  copy: (text: string) => Promise<boolean>;
  openLink: (url: string) => void;
  /** Called once the view has closed (focus can go back to the terminal). */
  onClose?: () => void;
}

interface Logical {
  text: string;
  /** First buffer row of this logical line (wrapped rows are joined). */
  row: number;
}

/** The active buffer as logical lines: rows the terminal soft-wrapped are
 * joined back up, so a long URL or command is one line again. */
function readBuffer(term: Terminal): { lines: Logical[]; truncated: boolean } {
  const buf = term.buffer.active;
  const out: Logical[] = [];
  for (let y = 0; y < buf.length; y++) {
    const line = buf.getLine(y);
    if (!line) continue;
    const next = buf.getLine(y + 1);
    // A row that continues onto the next keeps its trailing cells.
    const text = line.translateToString(!(next && next.isWrapped));
    if (line.isWrapped && out.length) out[out.length - 1].text += text;
    else out.push({ text, row: y });
  }
  while (out.length && !out[out.length - 1].text.trim()) out.pop();
  const truncated = out.length > MAX_LINES;
  return { lines: truncated ? out.slice(-MAX_LINES) : out, truncated };
}

/** Index of the logical line holding buffer *row*, and the string offset of
 * *col* within it. */
function locate(term: Terminal, lines: Logical[], row: number, col: number): { index: number; offset: number } | null {
  let index = -1;
  for (let i = lines.length - 1; i >= 0; i--) {
    if (lines[i].row <= row) { index = i; break; }
  }
  if (index < 0) return null;
  const buf = term.buffer.active;
  let offset = 0;
  for (let y = lines[index].row; y < row; y++) offset += buf.getLine(y)?.translateToString(false).length ?? 0;
  offset += buf.getLine(row)?.translateToString(false, 0, col).length ?? 0;
  return { index, offset };
}

function findLinks(lines: Logical[]): string[] {
  const seen = new Set<string>();
  const links: string[] = [];
  for (const { text } of lines) {
    for (const m of text.matchAll(URL_RE)) {
      const url = m[0].replace(URL_TRAIL_RE, "");
      if (url.length > 10 && !seen.has(url)) { seen.add(url); links.push(url); }
    }
  }
  return links.reverse();          // newest first
}

let openView: (() => void) | null = null;

/** Open the view. Returns a function that closes it. */
export function openSelectView(o: SelectViewOptions): () => void {
  openView?.();
  const { lines, truncated } = readBuffer(o.term);

  const root = h("div", "select-view");
  root.setAttribute("role", "dialog");
  root.setAttribute("aria-modal", "true");
  root.setAttribute("aria-label", "Select text");

  const bar = h("div", "select-view__bar");
  const title = h("span", "select-view__title", "select text");
  const copyBtn = h("button", "btn btn--primary select-view__btn", "copy");
  copyBtn.type = "button";
  copyBtn.disabled = true;
  const allBtn = h("button", "btn select-view__btn", "copy all");
  allBtn.type = "button";
  const links = findLinks(lines);
  const linksBtn = h("button", "btn select-view__btn", `links${links.length ? ` ${links.length}` : ""}`);
  linksBtn.type = "button";
  linksBtn.disabled = links.length === 0;
  linksBtn.setAttribute("aria-expanded", "false");
  const closeBtn = h("button", "btn select-view__close", "✕");
  closeBtn.type = "button";
  closeBtn.setAttribute("aria-label", "Close");
  bar.append(title, copyBtn, allBtn, linksBtn, closeBtn);

  const linkList = h("div", "select-view__links");
  linkList.hidden = true;
  for (const url of links) {
    const row = h("div", "select-view__link");
    const label = h("span", "select-view__link-url", url);
    const open = h("button", "btn select-view__btn", "open");
    open.type = "button";
    open.addEventListener("click", () => o.openLink(url));
    const copy = h("button", "btn select-view__btn", "copy");
    copy.type = "button";
    copy.addEventListener("click", () => { void doCopy(url); });
    row.append(label, open, copy);
    linkList.append(row);
  }

  const body = h("div", "select-view__body");
  if (truncated) body.append(h("div", "select-view__note", `the last ${MAX_LINES} lines`));
  const els = lines.map(({ text }) => {
    const el = h("div", "select-view__line", text || " ");
    body.append(el);
    return el;
  });

  const toast = h("div", "select-view__toast");
  toast.hidden = true;
  let toastTimer: number | null = null;
  const say = (text: string, ok = true): void => {
    toast.textContent = text;
    toast.classList.toggle("select-view__toast--err", !ok);
    toast.hidden = false;
    if (toastTimer !== null) clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => { toast.hidden = true; }, 1400);
  };
  const doCopy = async (text: string): Promise<void> => {
    if (!text) return;
    const ok = await o.copy(text);
    say(ok ? `copied ${text.length} char${text.length === 1 ? "" : "s"}` : "copy blocked by browser", ok);
  };

  root.append(bar, linkList, body, toast);
  document.body.append(root);

  const selected = (): string => {
    const sel = document.getSelection();
    if (!sel || sel.isCollapsed || !body.contains(sel.anchorNode)) return "";
    return sel.toString();
  };
  const onSelection = (): void => { copyBtn.disabled = !selected(); };
  document.addEventListener("selectionchange", onSelection);
  // mousedown on the buttons would clear the selection before click.
  for (const b of [copyBtn, allBtn]) b.addEventListener("mousedown", (e) => e.preventDefault());
  copyBtn.addEventListener("click", () => { void doCopy(selected()); });
  allBtn.addEventListener("click", () => { void doCopy(lines.map((l) => l.text.trimEnd()).join("\n")); });
  linksBtn.addEventListener("click", () => {
    linkList.hidden = !linkList.hidden;
    linksBtn.setAttribute("aria-expanded", String(!linkList.hidden));
  });

  // Open where the finger was, with that word selected. The finger is still
  // down: when it lifts, the browser's compatibility mouse events land on
  // this view's text and would clear the selection, so cancel that first
  // touchend and select after it (or after a moment, if it never comes).
  const at = locate(o.term, lines, o.row, o.col);
  const target = at ? els[at.index] : els[els.length - 1];
  requestAnimationFrame(() => target?.scrollIntoView({ block: "center" }));
  let preselected = false;
  const preselect = (): void => {
    if (preselected || closed) return;
    preselected = true;
    document.removeEventListener("touchend", onFirstUp, true);
    const node = target?.firstChild;
    if (at && node && node.nodeType === Node.TEXT_NODE) {
      const text = node.textContent ?? "";
      let a = Math.min(at.offset, text.length);
      let b = a;
      while (a > 0 && !/\s/.test(text[a - 1])) a--;
      while (b < text.length && !/\s/.test(text[b])) b++;
      if (b > a) {
        const range = document.createRange();
        range.setStart(node, a);
        range.setEnd(node, b);
        const sel = document.getSelection();
        sel?.removeAllRanges();
        sel?.addRange(range);
      }
    }
  };
  function onFirstUp(e: TouchEvent): void {
    if (e.cancelable) e.preventDefault();
    setTimeout(preselect, 0);
  }
  document.addEventListener("touchend", onFirstUp, { capture: true, passive: false });
  const preselectTimer = window.setTimeout(preselect, 1500);

  // Android's back button closes the view, not the app.
  let closed = false;
  history.pushState({ ccpipeSelectView: true }, "");
  const onPop = (): void => close(false);
  window.addEventListener("popstate", onPop);
  const onKey = (e: KeyboardEvent): void => {
    if (e.key === "Escape") { e.preventDefault(); close(); }
  };
  document.addEventListener("keydown", onKey, true);

  function close(popHistory = true): void {
    if (closed) return;
    closed = true;
    if (openView === close) openView = null;
    window.removeEventListener("popstate", onPop);
    document.removeEventListener("keydown", onKey, true);
    document.removeEventListener("selectionchange", onSelection);
    document.removeEventListener("touchend", onFirstUp, true);
    clearTimeout(preselectTimer);
    if (toastTimer !== null) clearTimeout(toastTimer);
    document.getSelection()?.removeAllRanges();
    root.remove();
    if (popHistory && history.state?.ccpipeSelectView) history.back();
    o.onClose?.();
  }
  closeBtn.addEventListener("click", () => close());
  openView = close;
  return close;
}
