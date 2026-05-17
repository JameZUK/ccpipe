// File transfer / management side-panel.
//
// Adaptive layout:
//   ≥ 600px viewport: anchored to the right edge of the screen, full
//                     height, with a draggable resize handle on its
//                     left. Width persists across sessions in
//                     localStorage so the user's preferred ratio
//                     between terminal and panel is sticky.
//   < 600px viewport: full-screen overlay (mobile pattern).
//
// Background backdrop is dimmed but not blocking — clicking outside
// the sheet dismisses, the rest of the terminal stays interactive
// underneath while the panel is open so the user can see what
// they're operating on.
//
// Hits the /api/fs/* endpoints for list + read + write + upload +
// download + rename + delete + mkdir. Inline editor is a plain
// <textarea> for v1 (UTF-8, ≤1 MB enforced server-side).

import { CLOSE_SVG, FOLDER_SVG, KEBAB_SVG } from "./icons";

const LS_WIDTH_KEY = "ccpipe.filePanelWidth";
const DEFAULT_WIDTH_PX = 480;
const MIN_WIDTH_PX = 320;
const MAX_WIDTH_VW = 0.9;     // up to 90% of viewport
// Below this viewport width the @media query in styles.css forces the
// panel full-width; the JS constant exists only as documentation of
// the breakpoint the CSS applies — referenced from comments above.

function loadStoredWidth(): number {
  try {
    const v = parseInt(localStorage.getItem(LS_WIDTH_KEY) ?? "", 10);
    if (Number.isFinite(v) && v >= MIN_WIDTH_PX) return v;
  } catch {}
  return DEFAULT_WIDTH_PX;
}

function saveStoredWidth(px: number): void {
  try { localStorage.setItem(LS_WIDTH_KEY, String(Math.round(px))); } catch {}
}

type FsEntry =
  | { name: string; type: "dir" }
  | { name: string; type: "file"; size: number; mtime: number };

type FsListResponse = {
  path: string;
  parent: string | null;
  entries: FsEntry[];
};

async function apiJson<T>(input: RequestInfo, init: RequestInit = {}): Promise<T> {
  const res = await fetch(input, {
    credentials: "same-origin",
    ...init,
    headers: {
      "Content-Type": "application/json",
      "X-Requested-By": "ccpipe",
      ...(init.headers ?? {}),
    },
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    throw new Error((detail as { detail?: string }).detail || `status ${res.status}`);
  }
  return (await res.json()) as T;
}

function fmtSize(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  return `${(n / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

function fmtTime(unix: number): string {
  const d = new Date(unix * 1000);
  return d.toLocaleString(undefined, {
    year: "numeric", month: "short", day: "2-digit",
    hour: "2-digit", minute: "2-digit",
  });
}

function joinPath(dir: string, name: string): string {
  return dir.endsWith("/") ? dir + name : dir + "/" + name;
}

export interface OpenFilePanelOptions {
  /** Initial cwd to show; defaults to /home. */
  initialPath?: string;
  /** Optional close hook fired when the user dismisses. */
  onClose?: () => void;
}

export function openFilePanel(parent: HTMLElement, opts: OpenFilePanelOptions = {}): void {
  const shell = document.createElement("div");
  shell.className = "file-panel-shell";
  shell.setAttribute("role", "dialog");
  shell.setAttribute("aria-label", "Files");

  const backdrop = document.createElement("div");
  backdrop.className = "file-panel-shell__backdrop";

  const sheet = document.createElement("div");
  sheet.className = "file-panel-shell__sheet file-panel";
  // Apply remembered width inline; the CSS @media query overrides this
  // on narrow viewports so the panel still goes full-screen on mobile.
  const startWidth = Math.min(loadStoredWidth(),
                               Math.floor(window.innerWidth * MAX_WIDTH_VW));
  sheet.style.width = `${startWidth}px`;

  // Drag handle on the LEFT edge — desktop only via CSS.
  const resize = document.createElement("div");
  resize.className = "file-panel-shell__resize";
  resize.setAttribute("aria-label", "Resize file panel");
  resize.setAttribute("role", "separator");
  resize.setAttribute("aria-orientation", "vertical");
  sheet.append(resize);

  // Wire pointer-driven resize. Each drag clamps to [MIN_WIDTH_PX, 90vw]
  // and writes the final value to localStorage on pointerup. We use
  // pointer capture so the drag survives the cursor sliding off the
  // 8px handle strip.
  let dragStartX = 0;
  let dragStartWidth = 0;
  // Tracks the user's last "wide" width so dbl-click snap-to-half can
  // toggle back to it on a second double-click. Initialised from the
  // loaded preference.
  let lastUserWidth = startWidth;
  resize.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    dragStartX = e.clientX;
    dragStartWidth = sheet.getBoundingClientRect().width;
    resize.setPointerCapture(e.pointerId);
    document.body.classList.add("file-panel-resizing");
  });
  resize.addEventListener("pointermove", (e) => {
    if (!resize.hasPointerCapture(e.pointerId)) return;
    const delta = dragStartX - e.clientX;            // drag-left = wider
    const max = Math.floor(window.innerWidth * MAX_WIDTH_VW);
    const next = Math.max(MIN_WIDTH_PX, Math.min(max, dragStartWidth + delta));
    sheet.style.width = `${next}px`;
  });
  const endDrag = (e: PointerEvent) => {
    if (!resize.hasPointerCapture(e.pointerId)) return;
    resize.releasePointerCapture(e.pointerId);
    document.body.classList.remove("file-panel-resizing");
    const w = sheet.getBoundingClientRect().width;
    saveStoredWidth(w);
    lastUserWidth = w;
  };
  resize.addEventListener("pointerup", endDrag);
  resize.addEventListener("pointercancel", endDrag);

  // Double-click the resize handle to snap the panel to 50% of the
  // viewport — and a second double-click flips back to the user's
  // last hand-set width. Useful for quick "side by side" layout
  // without dragging precisely.
  const halfWidth = () => Math.max(
    MIN_WIDTH_PX,
    Math.min(window.innerWidth * MAX_WIDTH_VW, Math.round(window.innerWidth * 0.5)),
  );
  resize.addEventListener("dblclick", (e) => {
    e.preventDefault();
    const cur = sheet.getBoundingClientRect().width;
    const half = halfWidth();
    const target = Math.abs(cur - half) < 4 ? lastUserWidth : half;
    // Brief CSS transition for the snap; cleared after so subsequent
    // drag pointermove updates feel instant.
    sheet.style.transition = "width 180ms var(--ease-out)";
    sheet.style.width = `${target}px`;
    window.setTimeout(() => { sheet.style.transition = ""; }, 220);
    saveStoredWidth(target);
    if (target !== half) lastUserWidth = target;
  });

  // ── Header ──────────────────────────────────────────────────────────
  const head = document.createElement("div");
  head.className = "file-panel__head";
  const title = document.createElement("div");
  title.className = "file-panel__title";
  title.textContent = "files";
  const closeBtn = document.createElement("button");
  closeBtn.type = "button";
  closeBtn.className = "btn btn--ghost btn--icon";
  closeBtn.title = "Close";
  closeBtn.innerHTML = CLOSE_SVG;
  head.append(title, closeBtn);

  // ── Path bar ────────────────────────────────────────────────────────
  const pathBar = document.createElement("form");
  pathBar.className = "file-panel__pathbar";
  const upBtn = document.createElement("button");
  upBtn.type = "button";
  upBtn.className = "btn btn--ghost file-panel__up";
  upBtn.title = "Parent";
  upBtn.textContent = "↑";
  const pathInput = document.createElement("input");
  pathInput.type = "text";
  pathInput.className = "file-panel__path";
  pathInput.spellcheck = false;
  pathInput.autocapitalize = "none";
  pathInput.autocomplete = "off";
  pathInput.value = opts.initialPath ?? "/home";
  const goBtn = document.createElement("button");
  goBtn.type = "submit";
  goBtn.className = "btn btn--ghost";
  goBtn.textContent = "go";
  pathBar.append(upBtn, pathInput, goBtn);

  // ── Toolbar (upload / new dir) ─────────────────────────────────────
  const toolbar = document.createElement("div");
  toolbar.className = "file-panel__toolbar";

  const uploadLabel = document.createElement("label");
  uploadLabel.className = "btn btn--primary file-panel__upload";
  uploadLabel.innerHTML = `<span>upload</span>`;
  const uploadInput = document.createElement("input");
  uploadInput.type = "file";
  uploadInput.hidden = true;
  uploadLabel.append(uploadInput);

  const mkdirBtn = document.createElement("button");
  mkdirBtn.type = "button";
  mkdirBtn.className = "btn btn--ghost";
  mkdirBtn.innerHTML = `${FOLDER_SVG}<span>new dir</span>`;

  const statusEl = document.createElement("div");
  statusEl.className = "file-panel__status";

  toolbar.append(uploadLabel, mkdirBtn, statusEl);

  // ── Listing ────────────────────────────────────────────────────────
  const list = document.createElement("div");
  list.className = "file-panel__list";

  sheet.append(head, pathBar, toolbar, list);
  shell.append(backdrop, sheet);
  parent.append(shell);

  // ── State ─────────────────────────────────────────────────────────
  let currentPath = opts.initialPath ?? "/home";
  let uploadLimitMb = 50;

  const setStatus = (msg: string, isError = false) => {
    statusEl.textContent = msg;
    statusEl.classList.toggle("file-panel__status--error", isError);
  };

  const dismiss = () => {
    shell.remove();
    document.removeEventListener("keydown", onKey);
    opts.onClose?.();
  };
  const onKey = (e: KeyboardEvent) => {
    if (e.key === "Escape") {
      e.preventDefault();
      dismiss();
    }
  };
  document.addEventListener("keydown", onKey);

  // Fetch upload cap once so client-side validation matches the
  // server's limit and the user sees the failure before transferring
  // megabytes that will be rejected anyway.
  void apiJson<{ upload_limit_mb: number }>("/api/fs/config").then((r) => {
    uploadLimitMb = r.upload_limit_mb || 50;
  }).catch(() => { /* keep default */ });

  const reload = async (path?: string) => {
    setStatus("loading…");
    list.innerHTML = "";
    try {
      const data = await apiJson<FsListResponse>(
        `/api/fs/list?path=${encodeURIComponent(path ?? currentPath)}&files=1&show_hidden=1`);
      currentPath = data.path;
      pathInput.value = data.path;
      upBtn.disabled = data.parent === null;
      renderEntries(data.entries);
      setStatus(`${data.entries.length} item${data.entries.length === 1 ? "" : "s"}`);
    } catch (err) {
      setStatus((err as Error).message, true);
    }
  };

  const renderEntries = (entries: FsEntry[]) => {
    list.innerHTML = "";
    if (entries.length === 0) {
      const empty = document.createElement("div");
      empty.className = "file-panel__empty";
      empty.textContent = "(empty)";
      list.append(empty);
      return;
    }
    for (const e of entries) {
      list.append(renderRow(e));
    }
  };

  const renderRow = (entry: FsEntry): HTMLElement => {
    const row = document.createElement("div");
    row.className = "file-panel__row file-panel__row--" + entry.type;

    const main = document.createElement("button");
    main.type = "button";
    main.className = "file-panel__row__main";
    const icon = document.createElement("span");
    icon.className = "file-panel__row__icon";
    icon.textContent = entry.type === "dir" ? "▸" : "·";
    const name = document.createElement("span");
    name.className = "file-panel__row__name";
    name.textContent = entry.name;
    main.append(icon, name);

    const meta = document.createElement("span");
    meta.className = "file-panel__row__meta";
    if (entry.type === "file") {
      meta.textContent = `${fmtSize(entry.size)} · ${fmtTime(entry.mtime)}`;
    } else {
      meta.textContent = "";
    }

    // Kebab menu — rename / delete (+ edit / download for files).
    const actions = document.createElement("div");
    actions.className = "file-panel__row__actions";
    const kebab = document.createElement("button");
    kebab.type = "button";
    kebab.className = "session-row__kebab";
    kebab.title = "Actions";
    kebab.innerHTML = KEBAB_SVG;
    const menu = document.createElement("div");
    menu.className = "session-row__menu";
    menu.hidden = true;
    actions.append(kebab, menu);

    const fullPath = joinPath(currentPath, entry.name);

    if (entry.type === "file") {
      menu.append(menuItem("edit", () => {
        closeMenu();
        openEditor(fullPath);
      }));
      menu.append(menuItem("download", () => {
        closeMenu();
        // Download via an anchor so the browser uses the
        // Content-Disposition filename from the server.
        const a = document.createElement("a");
        a.href = `/api/fs/download?path=${encodeURIComponent(fullPath)}`;
        a.download = entry.name;
        a.style.display = "none";
        document.body.append(a);
        a.click();
        setTimeout(() => a.remove(), 0);
      }));
    }
    menu.append(menuItem("rename", () => {
      closeMenu();
      promptRename(entry.name, fullPath);
    }));
    const killItem = document.createElement("button");
    killItem.type = "button";
    killItem.className = "session-row__menu__item danger";
    killItem.textContent = "delete";
    let killArmed = false;
    let killArmTimer: number | null = null;
    killItem.addEventListener("click", async (e) => {
      e.stopPropagation();
      if (!killArmed) {
        killArmed = true;
        killItem.textContent = `confirm delete ${entry.name}`;
        killItem.classList.add("armed");
        killArmTimer = window.setTimeout(() => {
          killArmed = false;
          killItem.textContent = "delete";
          killItem.classList.remove("armed");
        }, 5000);
        return;
      }
      if (killArmTimer !== null) clearTimeout(killArmTimer);
      closeMenu();
      try {
        await apiJson("/api/fs/delete", {
          method: "POST",
          body: JSON.stringify({ path: fullPath }),
        });
        await reload();
      } catch (err) {
        setStatus(`delete failed: ${(err as Error).message}`, true);
      }
    });
    menu.append(killItem);

    const openMenu = () => {
      list.querySelectorAll<HTMLElement>(".session-row__menu:not([hidden])")
        .forEach((m) => { m.hidden = true; });
      menu.classList.remove("session-row__menu--up");
      menu.hidden = false;
      const r = row.getBoundingClientRect();
      if (window.innerHeight - r.bottom < menu.offsetHeight + 16) {
        menu.classList.add("session-row__menu--up");
      }
    };
    const closeMenu = () => { menu.hidden = true; };
    kebab.addEventListener("click", (e) => {
      e.stopPropagation();
      if (menu.hidden) openMenu(); else closeMenu();
    });

    main.addEventListener("click", () => {
      if (entry.type === "dir") reload(fullPath);
      else openEditor(fullPath);
    });

    row.append(main, meta, actions);
    return row;
  };

  const menuItem = (label: string, onClick: () => void): HTMLElement => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "session-row__menu__item";
    b.textContent = label;
    b.addEventListener("click", (e) => { e.stopPropagation(); onClick(); });
    return b;
  };

  const promptRename = async (currentName: string, srcPath: string) => {
    const newName = window.prompt(`Rename "${currentName}" to:`, currentName);
    if (!newName || newName === currentName) return;
    if (newName.includes("/")) {
      setStatus("name cannot contain '/'", true);
      return;
    }
    const dst = joinPath(currentPath, newName);
    try {
      await apiJson("/api/fs/rename", {
        method: "POST",
        body: JSON.stringify({ src: srcPath, dst }),
      });
      await reload();
    } catch (err) {
      setStatus(`rename failed: ${(err as Error).message}`, true);
    }
  };

  // Upload — PUT raw bytes against /api/fs/upload?path=…
  uploadInput.addEventListener("change", async () => {
    const file = uploadInput.files?.[0];
    if (!file) return;
    if (file.size > uploadLimitMb * 1024 * 1024) {
      setStatus(`file exceeds upload limit (${uploadLimitMb} MB)`, true);
      uploadInput.value = "";
      return;
    }
    const target = joinPath(currentPath, file.name);
    setStatus(`uploading ${file.name} (${fmtSize(file.size)})…`);
    try {
      const res = await fetch(
        `/api/fs/upload?path=${encodeURIComponent(target)}`,
        {
          method: "POST",
          credentials: "same-origin",
          headers: {
            "X-Requested-By": "ccpipe",
            "Content-Type": file.type || "application/octet-stream",
          },
          body: file,
        });
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d.detail || `status ${res.status}`);
      }
      await reload();
    } catch (err) {
      setStatus(`upload failed: ${(err as Error).message}`, true);
    } finally {
      uploadInput.value = "";
    }
  });

  // New dir
  mkdirBtn.addEventListener("click", async () => {
    const name = window.prompt("new directory name:");
    if (!name || name.includes("/")) return;
    const target = joinPath(currentPath, name);
    try {
      await apiJson("/api/fs/mkdir", {
        method: "POST",
        body: JSON.stringify({ path: target }),
      });
      await reload();
    } catch (err) {
      setStatus(`mkdir failed: ${(err as Error).message}`, true);
    }
  });

  // Path bar
  upBtn.addEventListener("click", () => {
    const trimmed = currentPath.replace(/\/+$/, "");
    if (!trimmed || trimmed === "/") return;
    const idx = trimmed.lastIndexOf("/");
    reload(idx <= 0 ? "/" : trimmed.slice(0, idx));
  });
  pathBar.addEventListener("submit", (e) => {
    e.preventDefault();
    const v = pathInput.value.trim();
    if (v.startsWith("/")) reload(v);
  });

  closeBtn.addEventListener("click", dismiss);
  // Click the dimmed area outside the sheet → dismiss. Backdrop's
  // pointer-events: auto in CSS makes it capture clicks; the sheet
  // itself blocks its own clicks via pointer-events: auto as well.
  backdrop.addEventListener("click", dismiss);
  // Outside-click closes any open kebab menu.
  document.addEventListener("click", (e) => {
    list.querySelectorAll<HTMLElement>(".session-row__menu:not([hidden])")
      .forEach((m) => {
        const actions = m.parentElement;
        if (!actions || !actions.contains(e.target as Node)) m.hidden = true;
      });
  });

  void reload();
}

// ─── Inline editor ──────────────────────────────────────────────────

const LS_EDITOR_SIZE = "ccpipe.editorSize";
const LS_EDITOR_MAX  = "ccpipe.editorMaximised";

interface EditorSize { w: number; h: number; }

function loadEditorSize(): EditorSize | null {
  try {
    const raw = localStorage.getItem(LS_EDITOR_SIZE);
    if (!raw) return null;
    const v = JSON.parse(raw);
    if (typeof v?.w === "number" && typeof v?.h === "number") return v as EditorSize;
  } catch {}
  return null;
}

function saveEditorSize(s: EditorSize): void {
  try { localStorage.setItem(LS_EDITOR_SIZE, JSON.stringify(s)); } catch {}
}

function loadEditorMaximised(): boolean {
  try { return localStorage.getItem(LS_EDITOR_MAX) === "1"; } catch { return false; }
}

function saveEditorMaximised(v: boolean): void {
  try { localStorage.setItem(LS_EDITOR_MAX, v ? "1" : "0"); } catch {}
}

function openEditor(path: string): void {
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  overlay.setAttribute("role", "dialog");

  const sheet = document.createElement("div");
  sheet.className = "modal__sheet file-editor";
  // Apply persisted size. The CSS resize:both on .file-editor lets
  // the user drag the bottom-right corner; we observe the resulting
  // dimensions and save them so the next open restores the same shape.
  const stored = loadEditorSize();
  if (stored) {
    sheet.style.width  = `${stored.w}px`;
    sheet.style.height = `${stored.h}px`;
  }
  if (loadEditorMaximised()) sheet.dataset.maximised = "true";

  const head = document.createElement("div");
  head.className = "file-panel__head";
  const title = document.createElement("div");
  title.className = "file-panel__title";
  title.textContent = path;
  title.title = path;
  // Maximise / restore toggle. Two clicks to a full-viewport editor
  // when the user wants the most room, then click again to drop back
  // to the remembered size.
  const maxBtn = document.createElement("button");
  maxBtn.type = "button";
  maxBtn.className = "btn btn--ghost btn--icon";
  maxBtn.title = "Toggle maximise";
  const updateMaxIcon = () => {
    const on = sheet.dataset.maximised === "true";
    maxBtn.innerHTML = on
      ? `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8 3v3a2 2 0 0 1-2 2H3"/><path d="M21 8h-3a2 2 0 0 1-2-2V3"/><path d="M3 16h3a2 2 0 0 1 2 2v3"/><path d="M16 21v-3a2 2 0 0 1 2-2h3"/></svg>`
      : `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3h7v7H3z"/><path d="M14 3h7v7h-7z"/><path d="M14 14h7v7h-7z"/><path d="M3 14h7v7H3z"/></svg>`;
  };
  updateMaxIcon();
  maxBtn.addEventListener("click", () => {
    const on = sheet.dataset.maximised === "true";
    sheet.dataset.maximised = on ? "false" : "true";
    saveEditorMaximised(!on);
    updateMaxIcon();
  });
  const closeBtn = document.createElement("button");
  closeBtn.type = "button";
  closeBtn.className = "btn btn--ghost btn--icon";
  closeBtn.innerHTML = CLOSE_SVG;
  head.append(title, maxBtn, closeBtn);

  const textarea = document.createElement("textarea");
  textarea.className = "file-editor__area";
  textarea.spellcheck = false;
  textarea.autocapitalize = "none";

  const foot = document.createElement("div");
  foot.className = "file-editor__foot";
  const status = document.createElement("div");
  status.className = "file-panel__status";
  const saveBtn = document.createElement("button");
  saveBtn.type = "button";
  saveBtn.className = "btn btn--primary";
  saveBtn.textContent = "save";
  foot.append(status, saveBtn);

  // Three drag affordances: a 24px corner handle (both axes), and
  // separate right-edge / bottom-edge strips for width-only and
  // height-only drags. The independent edges are important because
  // a long edge is easier to hit on touch than a small corner, and
  // it splits the "drag for width" gesture out from the corner so
  // even if the corner handle is tricky there's still a way to widen.
  const mkHandle = (cls: string, label: string) => {
    const el = document.createElement("div");
    el.className = `file-editor__handle ${cls}`;
    el.setAttribute("aria-label", label);
    el.setAttribute("role", "separator");
    return el;
  };
  const cornerHandle = mkHandle("file-editor__handle--corner",  "Resize editor");
  const rightHandle  = mkHandle("file-editor__handle--right",   "Resize width");
  const bottomHandle = mkHandle("file-editor__handle--bottom",  "Resize height");
  sheet.append(head, textarea, foot, rightHandle, bottomHandle, cornerHandle);
  overlay.append(sheet);
  document.body.append(overlay);

  let dragStart = { x: 0, y: 0, w: 0, h: 0, axis: "both" as "both"|"w"|"h" };
  const clamp = () => ({
    minW: 480,
    minH: 320,
    maxW: Math.floor(window.innerWidth * 0.98),
    maxH: Math.floor(window.innerHeight * 0.98),
  });
  const wireHandle = (el: HTMLElement, axis: "both"|"w"|"h") => {
    el.addEventListener("pointerdown", (e) => {
      if (sheet.dataset.maximised === "true") return;
      e.preventDefault();
      const r = sheet.getBoundingClientRect();
      dragStart = { x: e.clientX, y: e.clientY, w: r.width, h: r.height, axis };
      el.setPointerCapture(e.pointerId);
      document.body.classList.add("file-editor-resizing");
    });
    el.addEventListener("pointermove", (e) => {
      if (!el.hasPointerCapture(e.pointerId)) return;
      const c = clamp();
      if (dragStart.axis !== "h") {
        const w = Math.max(c.minW, Math.min(c.maxW, dragStart.w + (e.clientX - dragStart.x)));
        sheet.style.width = `${w}px`;
      }
      if (dragStart.axis !== "w") {
        const h = Math.max(c.minH, Math.min(c.maxH, dragStart.h + (e.clientY - dragStart.y)));
        sheet.style.height = `${h}px`;
      }
    });
    const end = (e: PointerEvent) => {
      if (!el.hasPointerCapture(e.pointerId)) return;
      el.releasePointerCapture(e.pointerId);
      document.body.classList.remove("file-editor-resizing");
      saveEditorSize({
        w: Math.round(sheet.getBoundingClientRect().width),
        h: Math.round(sheet.getBoundingClientRect().height),
      });
    };
    el.addEventListener("pointerup", end);
    el.addEventListener("pointercancel", end);
  };
  wireHandle(cornerHandle, "both");
  wireHandle(rightHandle,  "w");
  wireHandle(bottomHandle, "h");

  const setStatus = (msg: string, error = false) => {
    status.textContent = msg;
    status.classList.toggle("file-panel__status--error", error);
  };

  // ResizeObserver tracks the user dragging the bottom-right resize
  // handle (CSS resize:both); we debounce and persist the result so
  // re-opening the editor lands at the same shape.
  let saveTimer: number | null = null;
  const ro = new ResizeObserver((entries) => {
    if (sheet.dataset.maximised === "true") return;
    const cr = entries[0].contentRect;
    if (saveTimer !== null) clearTimeout(saveTimer);
    saveTimer = window.setTimeout(() => {
      saveEditorSize({ w: Math.round(cr.width), h: Math.round(cr.height) });
    }, 250);
  });
  ro.observe(sheet);

  const dismiss = () => {
    try { ro.disconnect(); } catch {}
    overlay.remove();
  };

  // Load
  void apiJson<{ content: string; size: number }>(
    `/api/fs/read?path=${encodeURIComponent(path)}`,
  ).then((data) => {
    textarea.value = data.content;
    setStatus(`${fmtSize(data.size)} loaded`);
    textarea.focus();
  }).catch((err) => {
    setStatus(`load failed: ${(err as Error).message}`, true);
  });

  saveBtn.addEventListener("click", async () => {
    setStatus("saving…");
    saveBtn.disabled = true;
    try {
      await apiJson("/api/fs/write", {
        method: "POST",
        body: JSON.stringify({ path, content: textarea.value }),
      });
      setStatus("saved");
      setTimeout(dismiss, 700);
    } catch (err) {
      setStatus(`save failed: ${(err as Error).message}`, true);
    } finally {
      saveBtn.disabled = false;
    }
  });

  closeBtn.addEventListener("click", dismiss);
  const onKey = (e: KeyboardEvent) => {
    if (e.key === "Escape") {
      e.preventDefault();
      dismiss();
      document.removeEventListener("keydown", onKey);
    } else if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s") {
      e.preventDefault();
      saveBtn.click();
    }
  };
  document.addEventListener("keydown", onKey);
}
