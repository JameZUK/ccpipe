// The file viewer: what tapping a file in the file panel opens. Read-only,
// over the whole screen, with Edit to switch to the editor:
//
//   - text and code: coloured by language, with line numbers, a wrap
//     toggle and a show-tabs-and-spaces toggle (CodeMirror, read-only:
//     code-view.ts, shared with the editor);
//   - Markdown: rendered, by embedding the /view page, with a toggle to
//     the source;
//   - images (SVG too, shown through <img>, so its scripts never run):
//     fitted to the screen; pinch, wheel or double-tap to zoom, drag to
//     pan;
//   - PDF: the browser's own viewer in a frame, plus "open" for browsers
//     that won't show one there (iOS shows only the first page);
//   - audio and video: the browser's players (the server honours byte
//     ranges, so seeking works).
//
// ‹ › (or ← →, or a swipe on an image) step through the folder's files.
// ✕, Escape or the Android back button close it.

import { h } from "./dom";
import type { CodeView } from "./code-view";
import { fileKind, fmtSize, inlineUrl, type FileKind } from "./file-kinds";
import {
  ICON_DOWNLOAD, ICON_NEXT, ICON_OPEN, ICON_PENCIL, ICON_PREV, ICON_WHITESPACE, ICON_WRAP, ICON_X, iconNode,
} from "./icons";
import { mdViewUrl } from "./view-url";

export interface ViewerFile {
  name: string;
  path: string;
  size: number;
  mtime: number;
}

export interface FileViewerOptions {
  /** The folder's files, in the listing's order. */
  files: ViewerFile[];
  index: number;
  /** Project root, for the Markdown view's links. */
  root?: string;
  /** Open the editor on *f*; call *closed* once it has closed. */
  edit: (f: ViewerFile, closed: () => void) => void;
  download: (f: ViewerFile) => void;
  onClose?: () => void;
}

function fmtTime(unix: number): string {
  return new Date(unix * 1000).toLocaleString(undefined, {
    year: "numeric", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit",
  });
}

function iconButton(svg: string, label: string, cls = ""): HTMLButtonElement {
  const b = h("button", `btn btn--ghost btn--icon file-viewer__btn ${cls}`.trim());
  b.type = "button";
  b.title = label;
  b.setAttribute("aria-label", label);
  b.append(iconNode(svg));
  return b;
}

let openClose: (() => void) | null = null;

export function openFileViewer(o: FileViewerOptions): () => void {
  openClose?.();
  let index = o.index;
  let closed = false;
  let suspended = false;          // the editor is open on top
  let teardown: (() => void) | null = null;

  const root = h("div", "file-viewer");
  root.setAttribute("role", "dialog");
  root.setAttribute("aria-modal", "true");

  const bar = h("div", "file-viewer__bar");
  const prevBtn = iconButton(ICON_PREV, "Previous file (←)");
  const nextBtn = iconButton(ICON_NEXT, "Next file (→)");
  const titleBox = h("div", "file-viewer__title");
  const nameEl = h("span", "file-viewer__name");
  const metaEl = h("span", "file-viewer__meta");
  titleBox.append(nameEl, metaEl);
  const modeBtn = h("button", "btn btn--ghost file-viewer__mode");
  modeBtn.type = "button";
  const wrapBtn = iconButton(ICON_WRAP, "Wrap long lines");
  const wsBtn = iconButton(ICON_WHITESPACE, "Show tabs and spaces");
  const editBtn = iconButton(ICON_PENCIL, "Edit");
  const openBtn = iconButton(ICON_OPEN, "Open in a new tab");
  const dlBtn = iconButton(ICON_DOWNLOAD, "Download");
  const closeBtn = iconButton(ICON_X, "Close (Esc)");
  // ‹ › sit together on the right: top-left is where a phone's "back" is.
  bar.append(titleBox, prevBtn, nextBtn, modeBtn, wrapBtn, wsBtn, editBtn, openBtn, dlBtn, closeBtn);

  const body = h("div", "file-viewer__body");
  root.append(bar, body);
  document.body.append(root);

  let mdSource = false;            // Markdown: show the source instead of rendered
  let code: CodeView | null = null;  // the text view on screen
  let codePrefs: typeof import("./code-view") | null = null;
  // Wrap and whitespace are remembered on this device (shared with the
  // editor); read once the code view's chunk is in.
  let wrap = false, whitespace = false;

  const show = (): void => {
    teardown?.();
    teardown = null;
    body.replaceChildren();
    body.scrollTop = 0;
    const f = o.files[index];
    const kind = fileKind(f.name);
    nameEl.textContent = f.name;
    nameEl.title = f.path;
    metaEl.textContent = `${fmtSize(f.size)} · ${fmtTime(f.mtime)}`;
    prevBtn.disabled = index <= 0;
    nextBtn.disabled = index >= o.files.length - 1;
    const many = o.files.length > 1;
    prevBtn.hidden = nextBtn.hidden = !many;
    const textual = kind === "text" || (kind === "markdown" && mdSource);
    wrapBtn.hidden = wsBtn.hidden = !textual;
    wrapBtn.setAttribute("aria-pressed", String(wrap));
    wsBtn.setAttribute("aria-pressed", String(whitespace));
    editBtn.hidden = kind !== "text" && kind !== "markdown";
    modeBtn.hidden = kind !== "markdown";
    modeBtn.textContent = mdSource ? "rendered" : "source";
    modeBtn.title = mdSource ? "Show it rendered" : "Show the Markdown source";
    openBtn.hidden = !(kind === "pdf" || kind === "image" || kind === "video" || (kind === "markdown" && !mdSource));
    body.dataset.kind = textual ? "text" : kind;
    if (textual) void showText(f);
    else showMedia(f, kind);
  };

  const placeholder = (msg: string, f?: ViewerFile): void => {
    const box = h("div", "file-viewer__empty");
    box.append(h("div", "", msg));
    if (f) {
      const b = h("button", "btn btn--primary", "download");
      b.type = "button";
      b.addEventListener("click", () => o.download(f));
      box.append(b);
    }
    body.replaceChildren(box);
  };

  const showText = async (f: ViewerFile): Promise<void> => {
    const want = index;
    body.replaceChildren(h("div", "file-viewer__loading", "loading…"));
    let text: string;
    try {
      const res = await fetch(`/api/fs/read?path=${encodeURIComponent(f.path)}`, { credentials: "same-origin" });
      if (want !== index || closed) return;
      if (res.status === 415) { placeholder("No preview for this file: it isn't text.", f); return; }
      if (res.status === 413) { placeholder(`Too big to show here (${fmtSize(f.size)}).`, f); return; }
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        placeholder(`Couldn't open it: ${d.detail ?? `HTTP ${res.status}`}`);
        return;
      }
      text = (await res.json()).content as string;
    } catch {
      if (want === index) placeholder("Couldn't load it: connection lost.");
      return;
    }
    let cm: typeof import("./code-view");
    try {
      cm = await import("./code-view");
    } catch {
      if (want === index && !closed) placeholder("Couldn't load the code view: connection lost.", f);
      return;
    }
    if (want !== index || closed) return;
    if (!codePrefs) {
      codePrefs = cm;
      ({ wrap, whitespace } = cm.loadPrefs());
      wrapBtn.setAttribute("aria-pressed", String(wrap));
      wsBtn.setAttribute("aria-pressed", String(whitespace));
    }
    const box = h("div", "file-viewer__cm");
    body.replaceChildren(box);
    const cv = cm.createCodeView({ parent: box, text, name: f.name, readOnly: true });
    code = cv;
    teardown = () => { cv.destroy(); if (code === cv) code = null; };
    const n = cv.lines();
    const facts = (lang: string | null): string => [
      `${n.toLocaleString()} line${n === 1 ? "" : "s"}`, lang,
      cv.detected ? cm.indentLabel(cv.detected) : null,
      cv.lineEnding === "CRLF" ? "CRLF" : null, cv.bom ? "BOM" : null,
      fmtSize(f.size), fmtTime(f.mtime),
    ].filter(Boolean).join(" · ");
    metaEl.textContent = facts(null);
    void cv.language.then((lang) => { if (code === cv && lang) metaEl.textContent = facts(lang); });
  };

  const showMedia = (f: ViewerFile, kind: FileKind): void => {
    const src = inlineUrl(f.path);
    if (kind === "image") { teardown = showImage(body, src, f.name, step); return; }
    if (kind === "pdf") {
      const frame = h("iframe", "file-viewer__frame");
      frame.src = src;
      frame.title = f.name;
      body.append(frame);
      return;
    }
    if (kind === "markdown") {
      const frame = h("iframe", "file-viewer__frame file-viewer__frame--doc");
      frame.src = `${mdViewUrl(f.path, o.root)}&embed=1`;
      frame.title = f.name;
      body.append(frame);
      return;
    }
    const el = kind === "video" ? h("video", "file-viewer__media") : h("audio", "file-viewer__media");
    el.controls = true;
    el.preload = "metadata";
    if (el instanceof HTMLVideoElement) el.playsInline = true;
    el.src = src;
    el.addEventListener("error", () => {
      placeholder("This browser can't play this file.", f);
    });
    if (kind === "audio") {
      const box = h("div", "file-viewer__audio");
      box.append(h("div", "file-viewer__audio-name", f.name), el);
      body.append(box);
    } else {
      body.append(el);
    }
    teardown = () => { el.pause(); el.removeAttribute("src"); el.load(); };
  };

  const step = (d: number): void => {
    const next = index + d;
    if (next < 0 || next >= o.files.length) return;
    index = next;
    mdSource = false;
    show();
  };

  prevBtn.addEventListener("click", () => step(-1));
  nextBtn.addEventListener("click", () => step(1));
  wrapBtn.addEventListener("click", () => {
    wrap = !wrap;
    wrapBtn.setAttribute("aria-pressed", String(wrap));
    code?.setWrap(wrap);
    codePrefs?.savePrefs({ wrap });
  });
  wsBtn.addEventListener("click", () => {
    whitespace = !whitespace;
    wsBtn.setAttribute("aria-pressed", String(whitespace));
    code?.setWhitespace(whitespace);
    codePrefs?.savePrefs({ whitespace });
  });
  modeBtn.addEventListener("click", () => { mdSource = !mdSource; show(); });
  editBtn.addEventListener("click", () => {
    // The editor takes over; the viewer comes back when it closes, showing
    // what was saved.
    suspended = true;
    root.hidden = true;
    o.edit(o.files[index], () => {
      suspended = false;
      root.hidden = false;
      if (!closed) show();
    });
  });
  openBtn.addEventListener("click", () => {
    const f = o.files[index];
    const url = fileKind(f.name) === "markdown" ? mdViewUrl(f.path, o.root) : inlineUrl(f.path);
    window.open(url, "_blank", "noopener");
  });
  dlBtn.addEventListener("click", () => o.download(o.files[index]));
  closeBtn.addEventListener("click", () => close());

  const onKey = (e: KeyboardEvent): void => {
    if (suspended || e.defaultPrevented) return;
    const t = e.target as HTMLElement | null;
    // The code view's search bar has its own Escape (close the bar).
    if (t?.closest?.(".cm-panels")) return;
    if (e.key === "Escape") { e.preventDefault(); close(); return; }
    // Find: the code view's search covers the whole file; the browser's
    // only finds the lines drawn so far.
    if (code && (e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase() === "f") {
      e.preventDefault();
      code.openSearch();
      return;
    }
    if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
    if (e.key === "ArrowLeft") { e.preventDefault(); step(-1); }
    else if (e.key === "ArrowRight") { e.preventDefault(); step(1); }
  };
  document.addEventListener("keydown", onKey, true);

  // Android's back button closes the viewer, not the app.
  history.pushState({ ccpipeFileViewer: true }, "");
  const onPop = (): void => close(false);
  window.addEventListener("popstate", onPop);

  function close(popHistory = true): void {
    if (closed) return;
    closed = true;
    if (openClose === close) openClose = null;
    teardown?.();
    document.removeEventListener("keydown", onKey, true);
    window.removeEventListener("popstate", onPop);
    root.remove();
    if (popHistory && history.state?.ccpipeFileViewer) history.back();
    o.onClose?.();
  }
  openClose = close;
  show();
  return close;
}

/** An image fitted to *box*, with zoom (pinch, wheel, double-tap or
 * double-click) and pan (drag while zoomed). A horizontal swipe at the
 * fitted size calls *step* (±1). Returns a teardown. */
function showImage(box: HTMLElement, src: string, alt: string, step: (d: number) => void): () => void {
  const stage = h("div", "file-viewer__stage");
  const img = h("img", "file-viewer__img");
  img.src = src;
  img.alt = alt;
  img.draggable = false;
  const info = h("div", "file-viewer__img-info");
  stage.append(img, info);
  box.append(stage);
  img.addEventListener("load", () => { info.textContent = `${img.naturalWidth} × ${img.naturalHeight}`; });
  img.addEventListener("error", () => { info.textContent = "This browser can't show this image."; });

  let s = 1, tx = 0, ty = 0;
  const MAX = 10;
  const apply = (): void => {
    img.style.transform = `translate(${tx}px, ${ty}px) scale(${s})`;
    stage.classList.toggle("file-viewer__stage--zoomed", s > 1.001);
  };
  // Zoom to *ns* keeping the point (px, py) — relative to the stage's
  // centre, where the fitted image's centre is — where it is.
  const zoomAt = (ns: number, px: number, py: number): void => {
    ns = Math.min(MAX, Math.max(1, ns));
    tx = px - (px - tx) * (ns / s);
    ty = py - (py - ty) * (ns / s);
    s = ns;
    if (s <= 1.001) { s = 1; tx = 0; ty = 0; }
    apply();
  };
  const centreRel = (x: number, y: number): [number, number] => {
    const r = stage.getBoundingClientRect();
    return [x - (r.left + r.width / 2), y - (r.top + r.height / 2)];
  };

  stage.addEventListener("wheel", (e) => {
    e.preventDefault();
    const [px, py] = centreRel(e.clientX, e.clientY);
    zoomAt(s * Math.exp(-e.deltaY * 0.002), px, py);
  }, { passive: false });

  const pts = new Map<number, { x: number; y: number }>();
  let pinch: { d: number; mx: number; my: number } | null = null;
  // Where a one-finger (or mouse) press began: a quick horizontal swipe
  // at the fitted size steps to the next file; two taps toggle zoom.
  let down: { x: number; y: number; t: number } | null = null;
  let lastTap = 0;

  const toggleZoom = (x: number, y: number): void => {
    const [px, py] = centreRel(x, y);
    if (s > 1) zoomAt(1, 0, 0);
    else zoomAt(2.5, px, py);
  };

  stage.addEventListener("pointerdown", (e) => {
    stage.setPointerCapture(e.pointerId);
    pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pts.size === 2) {
      const [a, b] = [...pts.values()];
      pinch = { d: Math.hypot(a.x - b.x, a.y - b.y), mx: (a.x + b.x) / 2, my: (a.y + b.y) / 2 };
      down = null;
    } else if (pts.size === 1) {
      down = { x: e.clientX, y: e.clientY, t: performance.now() };
    }
  });
  stage.addEventListener("pointermove", (e) => {
    const prev = pts.get(e.pointerId);
    if (!prev) return;
    const cur = { x: e.clientX, y: e.clientY };
    pts.set(e.pointerId, cur);
    if (pinch && pts.size === 2) {
      const [a, b] = [...pts.values()];
      const d = Math.hypot(a.x - b.x, a.y - b.y);
      const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
      const [px, py] = centreRel(mx, my);
      tx += mx - pinch.mx;
      ty += my - pinch.my;
      zoomAt(s * (d / Math.max(1, pinch.d)), px, py);
      pinch = { d, mx, my };
    } else if (pts.size === 1 && s > 1) {
      tx += cur.x - prev.x;
      ty += cur.y - prev.y;
      apply();
    }
  });
  const up = (e: PointerEvent): void => {
    if (!pts.has(e.pointerId)) return;
    pts.delete(e.pointerId);
    if (pts.size < 2) pinch = null;
    if (pts.size > 0) return;
    const start = down;
    down = null;
    if (!start || e.type !== "pointerup") return;
    const dx = e.clientX - start.x, dy = e.clientY - start.y;
    if (s === 1 && performance.now() - start.t < 600
        && Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(dy) * 1.5) {
      step(dx < 0 ? 1 : -1);
    } else if (Math.abs(dx) < 10 && Math.abs(dy) < 10) {
      const now = performance.now();
      if (now - lastTap < 350) { toggleZoom(e.clientX, e.clientY); lastTap = 0; }
      else lastTap = now;
    }
  };
  stage.addEventListener("pointerup", up);
  stage.addEventListener("pointercancel", up);
  return () => { img.removeAttribute("src"); };
}
