import { openDirectoryBrowser } from "./directory-browser";
import { clearLastSession } from "./display-prefs";
import { FOLDER_SVG, GEAR_SVG, KEBAB_SVG } from "./icons";
import { loadRecentDirs, pushRecentDir } from "./recent-dirs";
import { openSettings } from "./settings";

type SessionInfo = {
  name: string;
  windows: number;
  attached: boolean;
  created: number;
};

type ClaudeSession = {
  id: string;
  mtime: number;
  size: number;
  firstUserMessage: string | null;
};

function relativeTime(unix: number): string {
  const diff = Math.floor(Date.now() / 1000) - unix;
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

function basename(path: string): string {
  const trimmed = path.replace(/\/+$/, "");
  const idx = trimmed.lastIndexOf("/");
  return idx >= 0 ? trimmed.slice(idx + 1) : trimmed;
}

function homeRelative(path: string): string {
  // We don't know $HOME on the client; the path bar shows the absolute
  // form everywhere, but recent-dir chips are tighter if we collapse
  // the user's home prefix to ~. The first chip we render comes from
  // localStorage which the user explicitly browsed to, so its prefix
  // matching against /home/<user> is a safe heuristic for shortening.
  const m = path.match(/^\/home\/[^/]+(\/.*)?$/);
  if (!m) return path;
  return "~" + (m[1] ?? "");
}

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

export function renderSessionPicker(
  root: HTMLElement,
  onPick: (session: string) => void,
): void {
  root.innerHTML = "";

  const frame = document.createElement("div");
  frame.className = "frame";

  const inner = document.createElement("div");
  inner.className = "frame__inner";

  const head = document.createElement("div");
  head.className = "frame__head";
  const word = document.createElement("div");
  word.className = "wordmark large";
  word.innerHTML = `cc<span class="dot"></span>pipe`;
  const tagline = document.createElement("div");
  tagline.className = "tagline";
  tagline.textContent = "select a session";
  const gear = document.createElement("button");
  gear.type = "button";
  gear.className = "frame__gear";
  gear.title = "Settings";
  gear.innerHTML = GEAR_SVG;
  gear.addEventListener("click", () => openSettings({
    authRequired: true,
    onDisplayPrefsChange: () => {},
    onSessionInvalidated: () => {
      clearLastSession();
      root.innerHTML = "";
      location.reload();
    },
  }));
  head.append(word, tagline, gear);

  const picker = document.createElement("div");
  picker.className = "picker";

  const list = document.createElement("div");
  list.className = "picker__list";

  const errBox = document.createElement("div");
  errBox.className = "error";

  // ─── New-session panel ────────────────────────────────────────────────
  const create = document.createElement("div");
  create.className = "picker__create";

  const createTitle = document.createElement("div");
  createTitle.className = "picker__create__label";
  createTitle.textContent = "start a new session";
  create.append(createTitle);

  // Working directory
  const cwdField = document.createElement("div");
  cwdField.className = "picker__field";
  const cwdLabel = document.createElement("label");
  cwdLabel.textContent = "working directory";
  const cwdRow = document.createElement("div");
  cwdRow.className = "picker__cwd-row";
  const cwdInput = document.createElement("input");
  cwdInput.type = "text";
  cwdInput.placeholder = "/home/you/projects/foo";
  cwdInput.spellcheck = false;
  cwdInput.autocapitalize = "none";
  cwdInput.autocomplete = "off";
  const browseBtn = document.createElement("button");
  browseBtn.type = "button";
  browseBtn.className = "btn btn--ghost";
  browseBtn.innerHTML = `${FOLDER_SVG}<span>browse…</span>`;
  cwdRow.append(cwdInput, browseBtn);
  const recentBox = document.createElement("div");
  recentBox.className = "picker__recent";
  cwdField.append(cwdLabel, cwdRow, recentBox);
  create.append(cwdField);

  // Resume list (hidden until a cwd resolves)
  const resumeBox = document.createElement("div");
  resumeBox.className = "picker__resume";
  resumeBox.hidden = true;
  create.append(resumeBox);

  // Session name
  const nameField = document.createElement("div");
  nameField.className = "picker__field";
  const nameLabel = document.createElement("label");
  nameLabel.textContent = "session name";
  const nameInput = document.createElement("input");
  nameInput.placeholder = "auto-filled from directory";
  nameInput.spellcheck = false;
  nameInput.autocapitalize = "none";
  nameInput.autocomplete = "off";
  nameField.append(nameLabel, nameInput);
  create.append(nameField);

  // Action row
  const actionRow = document.createElement("div");
  actionRow.className = "picker__create__actions";
  const startBtn = document.createElement("button");
  startBtn.type = "button";
  startBtn.className = "btn btn--primary";
  startBtn.textContent = "start session";
  actionRow.append(startBtn);
  create.append(actionRow);

  picker.append(list, errBox, create);
  inner.append(head, picker);
  frame.append(inner);
  root.append(frame);

  const showError = (msg: string) => { errBox.textContent = msg; };

  // ─── Recent dirs ────────────────────────────────────────────────────
  const renderRecent = () => {
    recentBox.innerHTML = "";
    const dirs = loadRecentDirs();
    if (dirs.length === 0) {
      recentBox.hidden = true;
      return;
    }
    recentBox.hidden = false;
    const lab = document.createElement("span");
    lab.className = "picker__recent__label";
    lab.textContent = "recent:";
    recentBox.append(lab);
    for (const d of dirs) {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "chip";
      chip.title = d;
      chip.textContent = homeRelative(d);
      chip.addEventListener("click", () => {
        cwdInput.value = d;
        onCwdChanged();
      });
      recentBox.append(chip);
    }
  };

  // ─── Resume list for the selected cwd ──────────────────────────────
  // Two states:
  //   (a) no resumeSelectedId set → Start button means "start fresh"
  //   (b) resumeSelectedId set    → Start button means "resume <id>"
  // Selecting a row toggles between them.
  let resumeSelectedId: string | null = null;

  const renderResumeList = (sessions: ClaudeSession[]) => {
    resumeBox.innerHTML = "";
    resumeSelectedId = null;
    if (sessions.length === 0) {
      resumeBox.hidden = true;
      startBtn.textContent = "start session";
      return;
    }
    resumeBox.hidden = false;

    const lab = document.createElement("div");
    lab.className = "picker__resume__label";
    lab.textContent =
      `resume an existing claude session in this dir ` +
      `(${sessions.length} found)`;
    const muted = document.createElement("div");
    muted.className = "picker__resume__hint";
    muted.textContent = "tap to select, or leave unselected to start fresh";
    resumeBox.append(lab, muted);

    for (const s of sessions) {
      const wrap = document.createElement("div");
      wrap.className = "resume-row-wrap";

      const row = document.createElement("button");
      row.type = "button";
      row.className = "resume-row";
      row.dataset.id = s.id;

      const preview = document.createElement("div");
      preview.className = "resume-row__preview";
      preview.textContent = s.firstUserMessage || "(no user prompt found)";

      const meta = document.createElement("div");
      meta.className = "resume-row__meta";
      meta.textContent =
        `${relativeTime(s.mtime)} • ${s.id.slice(0, 8)}`;

      row.append(preview, meta);
      row.addEventListener("click", () => {
        const wasSelected = resumeSelectedId === s.id;
        resumeBox.querySelectorAll(".resume-row.selected")
          .forEach((r) => r.classList.remove("selected"));
        if (wasSelected) {
          resumeSelectedId = null;
          startBtn.textContent = "start session";
        } else {
          row.classList.add("selected");
          resumeSelectedId = s.id;
          startBtn.textContent = "resume session";
        }
      });

      // Download-as-markdown button. Resolves to a .md file the user
      // can keep / share. Auth + cookies travel automatically with
      // same-origin GET; no preflight needed.
      const dl = document.createElement("a");
      dl.className = "resume-row__export";
      dl.title = "Export conversation as markdown";
      dl.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>`;
      dl.href = `/api/claude-sessions/${encodeURIComponent(s.id)}/export?cwd=${encodeURIComponent(cwdInput.value.trim())}`;
      dl.setAttribute("download", `ccpipe-${s.id.slice(0, 8)}.md`);
      dl.addEventListener("click", (e) => e.stopPropagation());

      wrap.append(row, dl);
      resumeBox.append(wrap);
    }
  };

  // ─── cwd-change handler: validates path, fetches resumable sessions,
  // and auto-fills the session name if the user hasn't typed one yet.
  let nameTouched = false;
  nameInput.addEventListener("input", () => { nameTouched = true; });

  const onCwdChanged = async () => {
    const cwd = cwdInput.value.trim();
    if (!cwd) {
      resumeBox.hidden = true;
      return;
    }
    if (!nameTouched) {
      nameInput.value = basename(cwd) || "session";
    }
    showError("");
    try {
      const data = await apiJson<{ sessions: ClaudeSession[] }>(
        `/api/claude-sessions?cwd=${encodeURIComponent(cwd)}`,
      );
      renderResumeList(data.sessions);
    } catch (err) {
      // Don't surface as an error — the cwd may be unreachable now
      // (e.g. typed but not yet finalised). Just hide the resume list.
      resumeBox.hidden = true;
    }
  };

  cwdInput.addEventListener("change", onCwdChanged);
  cwdInput.addEventListener("blur", onCwdChanged);

  browseBtn.addEventListener("click", () => {
    const start = cwdInput.value.trim() || "/home";
    openDirectoryBrowser(document.body, {
      initialPath: start,
      onPick: (p) => {
        cwdInput.value = p;
        onCwdChanged();
      },
    });
  });

  startBtn.addEventListener("click", async () => {
    const name = nameInput.value.trim();
    const cwd = cwdInput.value.trim();
    if (!name) { showError("name required"); return; }
    if (!cwd) { showError("working directory required"); return; }
    if (!cwd.startsWith("/")) { showError("cwd must be an absolute path"); return; }
    showError("");
    startBtn.disabled = true;
    try {
      const body: Record<string, string> = { name, cwd };
      if (resumeSelectedId) body.resumeSessionId = resumeSelectedId;
      await apiJson<SessionInfo>("/api/sessions", {
        method: "POST",
        body: JSON.stringify(body),
      });
      pushRecentDir(cwd);
      onPick(name);
    } catch (err) {
      showError(`failed to create: ${(err as Error).message}`);
    } finally {
      startBtn.disabled = false;
    }
  });

  // ─── Render existing sessions list ──────────────────────────────────
  const refresh = async () => {
    list.innerHTML = "";
    let sessions: SessionInfo[] = [];
    try {
      sessions = await apiJson<SessionInfo[]>("/api/sessions");
    } catch (err) {
      showError(`failed to list sessions: ${(err as Error).message}`);
      return;
    }
    if (sessions.length === 0) {
      const empty = document.createElement("div");
      empty.className = "picker__empty";
      empty.innerHTML = `
        <div class="picker__empty__icon">⌬</div>
        <div class="picker__empty__title">no sessions yet</div>
        <div class="picker__empty__hint">
          pick a project directory below and start your first claude session.
          existing transcripts in that dir will appear as resume options.
        </div>
      `;
      list.append(empty);
      list.className = "picker__list picker__list--empty";
      return;
    }
    list.className = "picker__list";
    sessions
      .slice()
      .sort((a, b) => Number(b.attached) - Number(a.attached) || b.created - a.created)
      .forEach((s, i) => list.append(renderSessionRow(s, i)));
  };

  const renderSessionRow = (s: SessionInfo, i: number): HTMLElement => {
    // The whole row is the click target so the visual hit area matches
    // the hover state. The kebab inside uses stopPropagation to keep
    // its menu interactions isolated from "open session".
    const row = document.createElement("div");
    row.className = "session-row";
    row.setAttribute("role", "button");
    row.setAttribute("tabindex", "0");
    row.setAttribute("aria-label", `Open session ${s.name}`);
    row.style.animation = `frame-in 380ms ${i * 30}ms var(--ease-out) backwards`;

    const dot = document.createElement("span");
    dot.className = "session-row__dot" + (s.attached ? " attached" : "");

    const body = document.createElement("div");
    body.className = "session-row__body";
    const nameEl = document.createElement("div");
    nameEl.className = "session-row__name";
    nameEl.textContent = s.name;
    const meta = document.createElement("div");
    meta.className = "session-row__meta";
    meta.innerHTML =
      `<span>${s.windows} window${s.windows === 1 ? "" : "s"}</span>` +
      `<span>${s.attached ? "attached" : `idle ${relativeTime(s.created)}`}</span>`;
    body.append(nameEl, meta);

    const actions = document.createElement("div");
    actions.className = "session-row__actions";

    const kebab = document.createElement("button");
    kebab.type = "button";
    kebab.className = "session-row__kebab";
    kebab.title = "Session actions";
    kebab.setAttribute("aria-label", `Actions for ${s.name}`);
    kebab.innerHTML = KEBAB_SVG;

    const menu = document.createElement("div");
    menu.className = "session-row__menu";
    menu.hidden = true;

    const renameItem = document.createElement("button");
    renameItem.type = "button";
    renameItem.className = "session-row__menu__item";
    renameItem.textContent = "rename";

    const killItem = document.createElement("button");
    killItem.type = "button";
    killItem.className = "session-row__menu__item danger";
    killItem.textContent = "kill";

    menu.append(renameItem, killItem);
    actions.append(kebab, menu);

    row.append(dot, body, actions);

    // Row-level click + Enter/Space open the session. Skip when the
    // click bubbles from the inline rename input or the kebab.
    const openSession = () => onPick(s.name);
    row.addEventListener("click", (e) => {
      if (actions.contains(e.target as Node)) return;
      if ((e.target as HTMLElement).closest(".session-row__rename")) return;
      openSession();
    });
    row.addEventListener("keydown", (e) => {
      if (e.target !== row) return;
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        openSession();
      }
    });

    // Open / close menu. Clicking anywhere else closes it.
    const closeMenu = () => { menu.hidden = true; };
    const openMenu = () => {
      list.querySelectorAll(".session-row__menu:not([hidden])")
        .forEach((m) => { (m as HTMLElement).hidden = true; });
      // Reveal first, then decide direction — offsetHeight is 0 while
      // the menu is still display:none, and we need a real measurement
      // to know if it'd render below the viewport.
      menu.classList.remove("session-row__menu--up");
      menu.hidden = false;
      const rect = row.getBoundingClientRect();
      const spaceBelow = window.innerHeight - rect.bottom;
      if (spaceBelow < menu.offsetHeight + 16) {
        menu.classList.add("session-row__menu--up");
      }
    };
    kebab.addEventListener("click", (e) => {
      e.stopPropagation();
      if (menu.hidden) openMenu(); else closeMenu();
    });
    document.addEventListener("click", (e) => {
      if (!actions.contains(e.target as Node)) closeMenu();
    });

    // ── Rename inline ──
    renameItem.addEventListener("click", (e) => {
      e.stopPropagation();
      closeMenu();
      startRename(row, s, nameEl);
    });

    // ── Kill with inline confirm ──
    let killConfirmTimer: number | null = null;
    let awaitingConfirm = false;
    const resetKill = () => {
      awaitingConfirm = false;
      killItem.textContent = "kill";
      killItem.classList.remove("armed");
      if (killConfirmTimer !== null) {
        clearTimeout(killConfirmTimer);
        killConfirmTimer = null;
      }
    };
    killItem.addEventListener("click", async (e) => {
      e.stopPropagation();
      if (!awaitingConfirm) {
        awaitingConfirm = true;
        killItem.textContent = `confirm kill ${s.name}`;
        killItem.classList.add("armed");
        killConfirmTimer = window.setTimeout(resetKill, 5000);
        return;
      }
      resetKill();
      closeMenu();
      try {
        await apiJson(`/api/sessions/${encodeURIComponent(s.name)}`, {
          method: "DELETE",
        });
        refresh();
      } catch (err) {
        showError(`failed to kill ${s.name}: ${(err as Error).message}`);
      }
    });

    return row;
  };

  // Inline rename: replaces the name span with an input. Enter commits,
  // Escape cancels. Validation errors are surfaced via the existing
  // errBox (above the create panel) so the user sees them without a
  // modal.
  const startRename = (_row: HTMLElement, s: SessionInfo, nameEl: HTMLElement) => {
    const input = document.createElement("input");
    input.type = "text";
    input.className = "session-row__rename";
    input.value = s.name;
    input.spellcheck = false;
    input.autocapitalize = "none";
    input.autocomplete = "off";
    nameEl.replaceWith(input);
    input.focus();
    input.select();

    const restore = () => {
      const restored = document.createElement("div");
      restored.className = "session-row__name";
      restored.textContent = s.name;
      input.replaceWith(restored);
    };

    const commit = async () => {
      const newName = input.value.trim();
      if (!newName || newName === s.name) {
        restore();
        return;
      }
      try {
        const updated = await apiJson<SessionInfo>(
          `/api/sessions/${encodeURIComponent(s.name)}`,
          { method: "PATCH", body: JSON.stringify({ newName }) },
        );
        s.name = updated.name;
        restore();
        refresh();
      } catch (err) {
        showError(`rename failed: ${(err as Error).message}`);
        restore();
      }
    };

    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); void commit(); }
      else if (e.key === "Escape") { e.preventDefault(); restore(); }
    });
    input.addEventListener("blur", () => void commit());
  };

  // First render
  renderRecent();
  refresh();
  // Default the cwd input to the user's home (a sensible starting point
  // from which Browse can navigate). The actual home string comes from
  // either the last recent dir or, failing that, /home (server side
  // will redirect to the user's home if they navigate up).
  const dirs = loadRecentDirs();
  cwdInput.value = dirs[0] ?? "/home";
  void onCwdChanged();
}
