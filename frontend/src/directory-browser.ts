// Modal directory browser: one-level navigation through the user's
// filesystem so they can pick where `claude` should run.
//
// We only render directories (not files) since the user is choosing a
// project root. Subdirectory entries are fetched on each navigation via
// GET /api/fs/list?path=... — caching would mask permission changes and
// the response is small enough to refetch cheaply.

import { CLOSE_SVG } from "./icons";

type FsListResponse = {
  path: string;
  parent: string | null;
  entries: Array<{ name: string }>;
};

export interface OpenDirectoryBrowserOptions {
  initialPath: string;
  /** Called with an absolute path when the user picks. */
  onPick: (absolutePath: string) => void;
  /** Called if the user dismisses without picking. */
  onCancel?: () => void;
}

export function openDirectoryBrowser(
  parent: HTMLElement,
  opts: OpenDirectoryBrowserOptions,
): void {
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  overlay.setAttribute("role", "dialog");
  overlay.setAttribute("aria-label", "Choose directory");

  const sheet = document.createElement("div");
  sheet.className = "modal__sheet dir-browser";

  // Header
  const head = document.createElement("div");
  head.className = "dir-browser__head";
  const title = document.createElement("div");
  title.className = "dir-browser__title";
  title.textContent = "choose directory";
  const closeBtn = document.createElement("button");
  closeBtn.type = "button";
  closeBtn.className = "btn btn--ghost btn--icon";
  closeBtn.innerHTML = CLOSE_SVG;
  closeBtn.title = "Close";
  head.append(title, closeBtn);

  // Path bar: editable text + up button
  const pathBar = document.createElement("form");
  pathBar.className = "dir-browser__pathbar";
  const upBtn = document.createElement("button");
  upBtn.type = "button";
  upBtn.className = "btn btn--ghost dir-browser__up";
  upBtn.textContent = "↑";
  upBtn.title = "Parent directory";
  const pathInput = document.createElement("input");
  pathInput.type = "text";
  pathInput.className = "dir-browser__input";
  pathInput.spellcheck = false;
  pathInput.autocapitalize = "none";
  pathInput.autocomplete = "off";
  pathInput.value = opts.initialPath;
  const goBtn = document.createElement("button");
  goBtn.type = "submit";
  goBtn.className = "btn btn--ghost dir-browser__go";
  goBtn.textContent = "go";
  pathBar.append(upBtn, pathInput, goBtn);

  // Entry list
  const list = document.createElement("div");
  list.className = "dir-browser__list";

  // Footer: status text + pick button
  const foot = document.createElement("div");
  foot.className = "dir-browser__foot";
  const status = document.createElement("div");
  status.className = "dir-browser__status";
  const pickBtn = document.createElement("button");
  pickBtn.type = "button";
  pickBtn.className = "btn btn--primary";
  pickBtn.textContent = "use this directory";
  foot.append(status, pickBtn);

  sheet.append(head, pathBar, list, foot);
  overlay.append(sheet);
  parent.append(overlay);

  let currentPath = opts.initialPath;
  let inFlight: AbortController | null = null;

  const setStatus = (msg: string, isError = false) => {
    status.textContent = msg;
    status.classList.toggle("error", isError);
  };

  const dismiss = (picked?: string) => {
    inFlight?.abort();
    overlay.remove();
    if (picked !== undefined) opts.onPick(picked);
    else opts.onCancel?.();
  };

  const load = async (path: string) => {
    inFlight?.abort();
    const ac = new AbortController();
    inFlight = ac;
    setStatus("loading…");
    list.innerHTML = "";
    let data: FsListResponse;
    try {
      const res = await fetch(
        `/api/fs/list?path=${encodeURIComponent(path)}`,
        { credentials: "same-origin", signal: ac.signal },
      );
      if (!res.ok) {
        const j = await res.json().catch(() => ({}));
        throw new Error(j.detail || `status ${res.status}`);
      }
      data = await res.json();
    } catch (err) {
      if ((err as Error).name === "AbortError") return;
      setStatus((err as Error).message, true);
      return;
    }

    currentPath = data.path;
    pathInput.value = data.path;
    upBtn.disabled = data.parent === null;

    if (data.entries.length === 0) {
      const empty = document.createElement("div");
      empty.className = "dir-browser__empty";
      empty.textContent = "(empty)";
      list.append(empty);
      setStatus(`no subdirectories in ${data.path}`);
      return;
    }

    for (const e of data.entries) {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "dir-browser__row";
      row.innerHTML = `<span class="dir-browser__icon">▸</span><span class="dir-browser__name"></span>`;
      row.querySelector(".dir-browser__name")!.textContent = e.name;
      row.addEventListener("click", () => {
        const next = data.path.endsWith("/")
          ? data.path + e.name
          : data.path + "/" + e.name;
        load(next);
      });
      list.append(row);
    }
    setStatus(`${data.entries.length} subdirector${data.entries.length === 1 ? "y" : "ies"}`);
  };

  upBtn.addEventListener("click", () => {
    // Compute parent locally so back-button works even on a synthetic
    // path the user typed but hasn't yet committed.
    const trimmed = currentPath.replace(/\/+$/, "");
    if (!trimmed || trimmed === "/") return;
    const idx = trimmed.lastIndexOf("/");
    const parentPath = idx <= 0 ? "/" : trimmed.slice(0, idx);
    load(parentPath);
  });

  pathBar.addEventListener("submit", (e) => {
    e.preventDefault();
    const v = pathInput.value.trim();
    if (!v) return;
    if (!v.startsWith("/")) {
      setStatus("path must be absolute", true);
      return;
    }
    load(v);
  });

  pickBtn.addEventListener("click", () => dismiss(currentPath));
  closeBtn.addEventListener("click", () => dismiss());
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) dismiss();
  });
  const onKey = (e: KeyboardEvent) => {
    if (e.key === "Escape") {
      e.preventDefault();
      dismiss();
      document.removeEventListener("keydown", onKey);
    }
  };
  document.addEventListener("keydown", onKey);

  load(opts.initialPath);
}
