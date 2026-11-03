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

import { apiJson } from "./api";
import { h } from "./dom";
import { fileKind, fmtSize, kindIcon } from "./file-kinds";
import { CLOSE_SVG, EYE_SVG, FOLDER_SVG, ICON_FOLDER, ICON_REFRESH, ICON_UPLOAD, ICON_X, KEBAB_SVG, iconNode } from "./icons";
import { inlineChoice, inlinePrompt } from "./inline-prompt";
import { clearFinished, downloadFile, onTransfers, uploadFiles, type Transfer } from "./transfers";
import { mdViewUrl } from "./view-url";

// Matches Markdown files for the "view rendered" affordances.
const MARKDOWN_RE = /\.(md|markdown)$/i;

/** Open the rendered-Markdown viewer for *absPath* in a new tab. */
function openMarkdownView(absPath: string, root?: string): void {
  window.open(mdViewUrl(absPath, root), "_blank", "noopener");
}

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

// apiJson moved to ./api.ts (shared with session-picker, settings, etc.)

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
  pathInput.value = opts.initialPath ?? "/";
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
  uploadLabel.append(iconNode(ICON_UPLOAD), h("span", "", "upload"));
  uploadLabel.title = "Upload files here (or drop them on the panel)";
  const uploadInput = document.createElement("input");
  uploadInput.type = "file";
  uploadInput.multiple = true;
  uploadInput.hidden = true;
  uploadLabel.append(uploadInput);

  const mkdirBtn = document.createElement("button");
  mkdirBtn.type = "button";
  mkdirBtn.className = "btn btn--ghost";
  mkdirBtn.innerHTML = `${FOLDER_SVG}<span>new dir</span>`;

  const statusEl = document.createElement("div");
  statusEl.className = "file-panel__status";

  // Download the folder being shown as a .zip.
  const zipBtn = document.createElement("button");
  zipBtn.type = "button";
  zipBtn.className = "btn btn--ghost";
  zipBtn.title = "Download this folder as a .zip";
  zipBtn.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3v12"/><path d="M7 10l5 5 5-5"/><path d="M5 21h14"/></svg><span>zip</span>`;

  toolbar.append(uploadLabel, mkdirBtn, zipBtn, statusEl);

  // ── Transfers (uploads / downloads in progress) ───────────────────
  const transfersEl = h("div", "file-panel__transfers");
  transfersEl.hidden = true;

  // ── Listing ────────────────────────────────────────────────────────
  const list = document.createElement("div");
  list.className = "file-panel__list";

  // Shown while files are dragged over the panel.
  const dropHint = h("div", "file-panel__drop", "drop to upload here");
  dropHint.hidden = true;

  sheet.append(head, pathBar, toolbar, transfersEl, list, dropHint);
  shell.append(backdrop, sheet);
  parent.append(shell);

  // ── State ─────────────────────────────────────────────────────────
  // Caller is expected to pass a sensible initialPath (typically the
  // jail root from /api/fs/config). "/" is the last-resort fallback —
  // it's almost certainly outside the jail, but the next /api/fs/list
  // 403 surfaces in the status line rather than silently failing.
  let currentPath = opts.initialPath ?? "/";
  let currentEntries: FsEntry[] = [];
  let uploadLimitMb = 50;

  const setStatus = (msg: string, isError = false) => {
    statusEl.textContent = msg;
    statusEl.classList.toggle("file-panel__status--error", isError);
  };

  const dismiss = () => {
    shell.remove();
    unsubTransfers();
    document.removeEventListener("keydown", onKey);
    document.removeEventListener("click", onDocClick);
    opts.onClose?.();
  };
  const onKey = (e: KeyboardEvent) => {
    // The viewer and editor, open over the panel, handle their own Escape.
    if (e.defaultPrevented || document.querySelector(".file-viewer, .file-editor")) return;
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
      currentEntries = data.entries;
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
    icon.append(iconNode(entry.type === "dir" ? ICON_FOLDER : kindIcon(entry.name)));
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

    if (entry.type === "dir") {
      menu.append(menuItem("download .zip", () => {
        closeMenu();
        void downloadZip(fullPath, entry.name);
      }));
    }
    if (entry.type === "file") {
      menu.append(menuItem("view", () => {
        closeMenu();
        void viewFile(entry.name);
      }));
      const kind = fileKind(entry.name);
      if (kind === "text" || kind === "markdown") {
        menu.append(menuItem("edit", () => {
          closeMenu();
          openEditor(fullPath, opts.initialPath);
        }));
      }
      menu.append(menuItem("download", () => {
        closeMenu();
        downloadFile(fullPath, entry.name, entry.size);
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
      else void viewFile(entry.name);
    });

    row.append(main, meta, actions);
    return row;
  };

  /** Open the viewer on *name*, stepping through this folder's files. */
  const viewFile = async (name: string): Promise<void> => {
    const files = currentEntries
      .filter((e): e is Extract<FsEntry, { type: "file" }> => e.type === "file")
      .map((e) => ({ name: e.name, path: joinPath(currentPath, e.name), size: e.size, mtime: e.mtime }));
    const index = files.findIndex((f) => f.name === name);
    if (index < 0) return;
    const { openFileViewer } = await import("./file-viewer");
    openFileViewer({
      files, index, root: opts.initialPath,
      edit: (f, closed) => openEditor(f.path, opts.initialPath, closed),
      download: (f) => downloadFile(f.path, f.name, f.size),
    });
  };

  /** Size the folder first ("120 files, 4.2 MB"), let the user include
   *  hidden files (.git, .venv, …) or not, then download it as a .zip. */
  const downloadZip = async (path: string, name: string): Promise<void> => {
    setStatus(`sizing ${name}…`);
    let s: {
      name: string; files: number; bytes: number;
      hidden_files: number; hidden_bytes: number; truncated: boolean;
    };
    try {
      s = await apiJson(`/api/fs/dir-summary?path=${encodeURIComponent(path)}`);
    } catch (err) {
      setStatus(`zip failed: ${(err as Error).message}`, true);
      return;
    }
    setStatus("");
    const zipName = `${s.name || name}.zip`;
    if (s.files === 0 && s.hidden_files === 0) {
      setStatus(`${name} has no files to download`);
      return;
    }
    const more = s.truncated ? "+" : "";
    const count = (n: number, b: number): string =>
      `${n.toLocaleString()}${more} file${n === 1 ? "" : "s"}, ${fmtSize(b)}${more}`;
    const choices = s.files > 0 ? ["Download"] : [];
    let detail = s.files > 0 ? count(s.files, s.bytes) : "Only hidden files in here.";
    if (s.hidden_files > 0) {
      choices.push("Include hidden files");
      detail += ` · hidden files (.git etc.): ${count(s.hidden_files, s.hidden_bytes)}`;
    }
    const pick = await inlineChoice(`Download ${zipName}?`, choices, detail);
    if (pick === null) return;
    const hidden = choices[pick] === "Include hidden files" ? 1 : 0;
    const a = document.createElement("a");
    a.href = `/api/fs/download-zip?path=${encodeURIComponent(path)}&hidden=${hidden}`;
    a.download = zipName;
    a.style.display = "none";
    document.body.append(a);
    a.click();
    setTimeout(() => a.remove(), 0);
    setStatus(`downloading ${zipName}…`);
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
    const newName = await inlinePrompt(`Rename "${currentName}" to:`, currentName);
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

  // Upload: any number of files, in resumable pieces (transfers.ts).
  const startUploads = async (picked: File[]): Promise<void> => {
    const dir = currentPath;
    let files = picked;
    const tooBig = files.filter((f) => f.size > uploadLimitMb * 1024 * 1024);
    if (tooBig.length) {
      setStatus(`over the ${uploadLimitMb} MB upload limit, skipped: ${tooBig.map((f) => f.name).join(", ")}`, true);
      files = files.filter((f) => !tooBig.includes(f));
    }
    const here = new Set(currentEntries.filter((e) => e.type === "file").map((e) => e.name));
    const clash = files.filter((f) => here.has(f.name));
    if (clash.length) {
      const pick = await inlineChoice(
        clash.length === 1 ? `${clash[0].name} is already here` : `${clash.length} of these files are already here`,
        ["Replace", clash.length === files.length ? "Cancel" : "Skip them"],
        clash.length === 1 ? "" : clash.map((f) => f.name).join(", ").slice(0, 400));
      if (pick === null) return;
      if (pick === 1) files = files.filter((f) => !here.has(f.name));
    }
    if (files.length) uploadFiles(dir, files);
  };
  uploadInput.addEventListener("change", () => {
    const files = [...(uploadInput.files ?? [])];
    uploadInput.value = "";
    if (files.length) void startUploads(files);
  });

  // Drag and drop onto the panel.
  let dragDepth = 0;
  const hasFiles = (e: DragEvent): boolean => !!e.dataTransfer && [...e.dataTransfer.types].includes("Files");
  sheet.addEventListener("dragenter", (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    dragDepth++;
    dropHint.hidden = false;
  });
  sheet.addEventListener("dragover", (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    e.dataTransfer!.dropEffect = "copy";
  });
  sheet.addEventListener("dragleave", (e) => {
    if (!hasFiles(e)) return;
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) dropHint.hidden = true;
  });
  sheet.addEventListener("drop", (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    dragDepth = 0;
    dropHint.hidden = true;
    const items = [...(e.dataTransfer!.items ?? [])];
    const folders = items.filter((it) => it.webkitGetAsEntry?.()?.isDirectory).length;
    const files = items
      .filter((it) => it.kind === "file" && !it.webkitGetAsEntry?.()?.isDirectory)
      .map((it) => it.getAsFile())
      .filter((f): f is File => f !== null);
    if (folders) setStatus(`folders aren't uploaded (${folders} skipped): zip them first`, true);
    if (files.length) void startUploads(files);
  });

  // The transfer list, and a refresh when an upload lands in this folder.
  const seenDone = new Set<number>();
  let refreshTimer: number | null = null;
  const unsubTransfers = onTransfers((ts) => {
    renderTransfers(transfersEl, ts);
    for (const t of ts) {
      if (t.state !== "done" || seenDone.has(t.id)) continue;
      seenDone.add(t.id);
      const dir = t.path.slice(0, t.path.lastIndexOf("/")) || "/";
      if (t.kind === "upload" && dir === currentPath.replace(/\/+$/, "")) {
        if (refreshTimer !== null) clearTimeout(refreshTimer);
        refreshTimer = window.setTimeout(() => { refreshTimer = null; void reload(); }, 300);
      }
    }
  });

  // New dir
  zipBtn.addEventListener("click", () => {
    const name = currentPath.replace(/\/+$/, "").split("/").pop() || "files";
    void downloadZip(currentPath, name);
  });

  mkdirBtn.addEventListener("click", async () => {
    const name = await inlinePrompt("New directory name:");
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
  // Outside-click closes any open kebab menu. Captured in a const so
  // the dismiss() path can detach it — without this every open/close
  // cycle of the file panel adds a fresh document listener that
  // outlives the panel and fires on every click for the page's life.
  const onDocClick = (e: MouseEvent) => {
    list.querySelectorAll<HTMLElement>(".session-row__menu:not([hidden])")
      .forEach((m) => {
        const actions = m.parentElement;
        if (!actions || !actions.contains(e.target as Node)) m.hidden = true;
      });
  };
  document.addEventListener("click", onDocClick);

  void reload();
}

// ─── Transfer list ──────────────────────────────────────────────────

function fmtRate(bps: number): string {
  return bps > 0 ? `${fmtSize(Math.round(bps))}/s` : "";
}

/** The uploads and downloads (transfers.ts), newest first, with progress,
 * what's going on when a link is bad, and cancel / retry. */
function renderTransfers(box: HTMLElement, ts: readonly Transfer[]): void {
  box.hidden = ts.length === 0;
  box.replaceChildren();
  if (!ts.length) return;
  const head = h("div", "file-panel__transfers-head");
  const live = ts.filter((t) => t.state === "queued" || t.state === "running" || t.state === "retrying").length;
  head.append(h("span", "", live ? `${live} transfer${live === 1 ? "" : "s"} running` : "transfers"));
  if (ts.some((t) => t.state === "done" || t.state === "cancelled")) {
    const clear = h("button", "btn btn--ghost file-panel__transfers-clear", "clear finished");
    clear.type = "button";
    clear.addEventListener("click", () => clearFinished());
    head.append(clear);
  }
  box.append(head);
  for (const t of [...ts].reverse()) {
    const row = h("div", `file-panel__transfer file-panel__transfer--${t.state}`);
    const top = h("div", "file-panel__transfer-top");
    const arrow = h("span", "file-panel__transfer-dir", t.kind === "upload" ? "↑" : "↓");
    const name = h("span", "file-panel__transfer-name", t.name);
    name.title = t.path;
    const pct = t.size > 0 ? Math.min(100, (t.done / t.size) * 100) : (t.state === "done" ? 100 : 0);
    let what: string;
    switch (t.state) {
      case "queued": what = "waiting"; break;
      case "done": what = `${fmtSize(t.size)} · done`; break;
      case "cancelled": what = "cancelled"; break;
      case "failed": what = `failed at ${fmtSize(t.done)}`; break;
      default: what = `${fmtSize(t.done)} / ${fmtSize(t.size)}${t.rate ? ` · ${fmtRate(t.rate)}` : ""}`;
    }
    const stat = h("span", "file-panel__transfer-stat", what);
    top.append(arrow, name, stat);
    if (t.state === "failed") {
      const retry = h("button", "btn btn--ghost btn--icon file-panel__transfer-btn");
      retry.type = "button";
      retry.title = "Try again from where it stopped";
      retry.append(iconNode(ICON_REFRESH));
      retry.addEventListener("click", () => t.retry());
      top.append(retry);
    }
    if (t.state === "queued" || t.state === "running" || t.state === "retrying" || t.state === "failed") {
      const cancel = h("button", "btn btn--ghost btn--icon file-panel__transfer-btn");
      cancel.type = "button";
      cancel.title = "Cancel";
      cancel.append(iconNode(ICON_X));
      cancel.addEventListener("click", () => t.cancel());
      top.append(cancel);
    }
    const bar = h("div", "file-panel__transfer-bar");
    const fill = h("div", "file-panel__transfer-fill");
    fill.style.width = `${pct.toFixed(1)}%`;
    bar.append(fill);
    row.append(top, bar);
    if (t.note) row.append(h("div", "file-panel__transfer-note", t.note));
    box.append(row);
  }
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

/** *onClosed* runs once the editor has closed (the viewer reloads). */
function openEditor(path: string, root?: string, onClosed?: () => void): void {
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  overlay.setAttribute("role", "dialog");

  const sheet = document.createElement("div");
  // No modal__sheet class — that class caps max-width at 32rem and
  // max-height at 80vh (later in the stylesheet, same specificity),
  // which silently overrode .file-editor's own max-width: 98vw and
  // made the right-edge / corner drag handles look broken.
  sheet.className = "file-editor";
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
  // Markdown files get a "preview" eye that opens the rendered viewer in
  // a new tab, so the edit pane and the rendered output sit side by side.
  if (MARKDOWN_RE.test(path)) {
    const previewBtn = document.createElement("button");
    previewBtn.type = "button";
    previewBtn.className = "btn btn--ghost btn--icon";
    previewBtn.title = "Open rendered preview in a new tab";
    previewBtn.insertAdjacentHTML("afterbegin", EYE_SVG);  // trusted static icon
    previewBtn.addEventListener("click", () => openMarkdownView(path, root));
    head.append(title, previewBtn, maxBtn, closeBtn);
  } else {
    head.append(title, maxBtn, closeBtn);
  }

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
    // Derive the minimums from the viewport so a 481–599px device (small
    // tablet / large phone, below the editor's full-screen breakpoint
    // but narrower than a hardcoded 480) can't be forced wider than its
    // own screen.
    minW: Math.min(480, Math.floor(window.innerWidth * 0.9)),
    minH: Math.min(320, Math.floor(window.innerHeight * 0.9)),
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

  // Baseline for the dirty check — the content as loaded (and as last
  // saved). textarea.value diverging from this means unsaved edits.
  let savedContent: string | null = null;
  const isDirty = (): boolean =>
    savedContent !== null && textarea.value !== savedContent;

  const dismiss = (force = false): void => {
    // Guard against losing unsaved edits on close / Escape / backdrop.
    if (!force && isDirty()
        && !window.confirm("Discard unsaved changes?")) {
      return;
    }
    try { ro.disconnect(); } catch {}
    document.removeEventListener("keydown", onKey);
    overlay.remove();
    onClosed?.();
  };

  // Load
  void apiJson<{ content: string; size: number }>(
    `/api/fs/read?path=${encodeURIComponent(path)}`,
  ).then((data) => {
    textarea.value = data.content;
    savedContent = data.content;
    setStatus(`${fmtSize(data.size)} loaded`);
    textarea.focus();
  }).catch((err) => {
    setStatus(`load failed: ${(err as Error).message}`, true);
  });

  saveBtn.addEventListener("click", async () => {
    setStatus("saving…");
    saveBtn.disabled = true;
    try {
      const justSaved = textarea.value;
      await apiJson("/api/fs/write", {
        method: "POST",
        body: JSON.stringify({ path, content: justSaved }),
      });
      savedContent = justSaved;        // no longer dirty
      setStatus("saved");
      setTimeout(() => dismiss(true), 700);
    } catch (err) {
      setStatus(`save failed: ${(err as Error).message}`, true);
    } finally {
      saveBtn.disabled = false;
    }
  });

  closeBtn.addEventListener("click", () => dismiss());
  function onKey(e: KeyboardEvent): void {
    if (e.key === "Escape") {
      e.preventDefault();
      // dismiss() removes this listener itself once it actually closes;
      // don't remove it here or a cancelled discard-confirm would leave
      // Escape dead for the rest of the session.
      dismiss();
    } else if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s") {
      e.preventDefault();
      saveBtn.click();
    }
  }
  document.addEventListener("keydown", onKey);
}
