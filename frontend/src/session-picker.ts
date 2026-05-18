import { apiJson, getFsConfig } from "./api";
import { openDirectoryBrowser } from "./directory-browser";
import { clearLastSession } from "./display-prefs";
import { FOLDER_SVG, GEAR_SVG, KEBAB_SVG, PIN_SVG } from "./icons";
import { loadRecentDirs, pushRecentDir } from "./recent-dirs";
// `./settings` is dynamically imported in the gear click handler so it
// stays out of the session-picker chunk.

type SessionInfo = {
  name: string;
  windows: number;
  attached: boolean;
  created: number;
  // True if the backend has this session in its sticky map, i.e. it
  // will be auto-recreated on backend restart. Drives the pin glyph
  // on the row and the kebab menu's toggle label.
  sticky: boolean;
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

// apiJson moved to ./api.ts

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
  gear.addEventListener("click", async () => {
    const { openSettings } = await import("./settings");
    openSettings({
      authRequired: true,
      onDisplayPrefsChange: () => {},
      onSessionInvalidated: () => {
        clearLastSession();
        root.innerHTML = "";
        location.reload();
      },
    });
  });
  head.append(word, tagline, gear);

  // ─── Backend reachability indicator ────────────────────────────────
  // Same conceptual role as the per-session statusbar's dot + latency
  // pip, but here there's no WS yet — we poll /api/health periodically
  // so the user knows the backend is reachable from the device they're
  // on BEFORE picking a session. Dot colour mirrors latency buckets:
  // green <100ms, amber <300ms, red on failure / timeout.
  const statusRow = document.createElement("div");
  statusRow.className = "frame__status";
  const statusDot = document.createElement("span");
  statusDot.className = "frame__status__dot";
  const statusLabel = document.createElement("span");
  statusLabel.className = "frame__status__label";
  statusLabel.textContent = "checking…";
  const statusLatency = document.createElement("span");
  statusLatency.className = "frame__status__latency";
  statusRow.append(statusDot, statusLabel, statusLatency);

  const setStatus = (cls: "ok" | "warn" | "err" | "",
                      label: string, latency: string,
                      bucket: "ok" | "warn" | "bad" | "" = "") => {
    statusDot.className = "frame__status__dot" + (cls ? " " + cls : "");
    statusLabel.textContent = label;
    statusLatency.textContent = latency;
    statusLatency.className = "frame__status__latency"
      + (bucket ? " frame__status__latency--" + bucket : "");
    statusLatency.hidden = !latency;
  };

  const HEALTH_POLL_MS = 5000;
  // Cap on a single in-flight probe — long enough to be patient with a
  // sluggish backend, short enough to flip the dot red within one
  // tick if something's really wrong.
  const HEALTH_TIMEOUT_MS = 4000;
  let activeProbe: AbortController | null = null;
  let healthTimer: number | null = null;

  const probeHealth = async () => {
    // Cancel any in-flight probe — if we're already overdue, the new
    // one wins. Without this a stalled probe stacks behind the next
    // tick and the UI lags.
    activeProbe?.abort();
    const ac = new AbortController();
    activeProbe = ac;
    const timeout = window.setTimeout(() => ac.abort(), HEALTH_TIMEOUT_MS);
    const t0 = performance.now();
    try {
      const res = await fetch("/api/health", {
        credentials: "same-origin",
        signal: ac.signal,
      });
      const rtt = Math.round(performance.now() - t0);
      if (!res.ok) {
        setStatus("err", "backend " + res.status, "");
        return;
      }
      const bucket: "ok" | "warn" | "bad" =
        rtt < 100 ? "ok" : rtt < 300 ? "warn" : "bad";
      // Dot follows the bucket so a single glance answers "is this
      // device close to the server?". On a fast LAN expect <30ms,
      // green; on cellular expect 100-300ms, amber.
      const dotCls: "ok" | "warn" | "err" =
        bucket === "bad" ? "err" : bucket === "warn" ? "warn" : "ok";
      setStatus(dotCls, "online", `${rtt} ms`, bucket);
    } catch (err) {
      // AbortError is either the explicit timeout above or a follow-on
      // tick superseding this one — either way, the next tick will
      // tell the real story. Don't flicker the dot for an abort.
      if ((err as Error)?.name === "AbortError") return;
      setStatus("err", "offline", "");
    } finally {
      clearTimeout(timeout);
      if (activeProbe === ac) activeProbe = null;
    }
  };

  // Start probing AFTER the event loop drains, then re-tick on an
  // interval. Both timers are stopped automatically once the picker
  // is detached from the DOM — frame.isConnected goes false when
  // attachTerminal wipes root, so we don't need a separate dispose
  // hook.
  //
  // Why requestIdleCallback for the first probe: a synchronous
  // `void probeHealth()` here would fire while the picker UI is
  // still mid-mount + JS bundles still parsing — fetch initiates
  // immediately but the Promise resolution that finally records
  // `rtt = performance.now() - t0` is a microtask, queued behind
  // page-boot work, which inflates the first reading by 5-10ms
  // even though the network is fine. Yielding to idle lets the
  // boot work clear, and the first reading is honest. See
  // [[feedback-rtt-measurement-pitfalls]] for the broader pattern.
  const fireFirstProbe = () => { void probeHealth(); };
  if (typeof window.requestIdleCallback === "function") {
    window.requestIdleCallback(fireFirstProbe, { timeout: 500 });
  } else {
    setTimeout(fireFirstProbe, 0);
  }
  healthTimer = window.setInterval(() => {
    if (!frame.isConnected) {
      if (healthTimer !== null) clearInterval(healthTimer);
      healthTimer = null;
      activeProbe?.abort();
      return;
    }
    void probeHealth();
  }, HEALTH_POLL_MS);

  const picker = document.createElement("div");
  picker.className = "picker";

  const list = document.createElement("div");
  list.className = "picker__list";

  const errBox = document.createElement("div");
  errBox.className = "error";

  // ─── New-session panel ────────────────────────────────────────────────
  // Collapsed-by-default accordion. The "start a new session" toggle
  // lives below the list; clicking it expands the form below. Keeps
  // the picker tight when the user is just looking for an existing
  // session, and gives the form full breathing room when needed.
  const create = document.createElement("div");
  create.className = "picker__create";
  create.dataset.expanded = "false";

  const createToggle = document.createElement("button");
  createToggle.type = "button";
  createToggle.className = "picker__create__toggle";
  createToggle.setAttribute("aria-expanded", "false");
  createToggle.innerHTML = `
    <span class="picker__create__plus" aria-hidden="true">+</span>
    <span class="picker__create__toggle-label">start a new session</span>
    <span class="picker__create__chev" aria-hidden="true">▾</span>
  `;
  create.append(createToggle);

  // CSS grid-template-rows trick: animate 0fr ↔ 1fr to expand without
  // measuring content height manually. Body lives inside an overflow:
  // hidden wrap so contents don't peek during the transition.
  const createBodyWrap = document.createElement("div");
  createBodyWrap.className = "picker__create__body-wrap";
  const createBody = document.createElement("div");
  createBody.className = "picker__create__body";
  createBodyWrap.append(createBody);
  create.append(createBodyWrap);

  createToggle.addEventListener("click", () => {
    const next = create.dataset.expanded !== "true";
    create.dataset.expanded = next ? "true" : "false";
    createToggle.setAttribute("aria-expanded", next ? "true" : "false");
    if (next) {
      // Focus the cwd input once the expand animation has run.
      setTimeout(() => cwdInput.focus(), 220);
    }
  });

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
  createBody.append(cwdField);

  // Resume list (hidden until a cwd resolves)
  const resumeBox = document.createElement("div");
  resumeBox.className = "picker__resume";
  resumeBox.hidden = true;
  createBody.append(resumeBox);

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
  createBody.append(nameField);

  // Action row
  const actionRow = document.createElement("div");
  actionRow.className = "picker__create__actions";
  const startBtn = document.createElement("button");
  startBtn.type = "button";
  startBtn.className = "btn btn--primary";
  startBtn.textContent = "start session";
  actionRow.append(startBtn);
  createBody.append(actionRow);

  picker.append(list, errBox, create);
  inner.append(head, statusRow, picker);
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

  browseBtn.addEventListener("click", async () => {
    // Resolve the fs jail root once so the directory browser opens
    // inside it. "/home" used to be hardcoded here but it's the
    // parent of the default root and gets refused by the jail check.
    let start = cwdInput.value.trim();
    if (!start) {
      try { start = (await getFsConfig()).root; }
      catch { start = "/"; }
    }
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

    // Single-line layout: name takes the available width and truncates
    // with an ellipsis; status sits to its right tightly. The old
    // two-line meta block has been folded into the status pill.
    // Name + pin share one grid cell via a flex wrapper so the grid
    // template stays 4 columns regardless of sticky state — putting
    // the pin into its own grid track would wrap the actions cell to
    // a new row on smaller widths and the kebab would land bottom-left.
    const nameWrap = document.createElement("div");
    nameWrap.className = "session-row__name-wrap";
    const nameEl = document.createElement("div");
    nameEl.className = "session-row__name";
    nameEl.textContent = s.name;
    nameWrap.append(nameEl);
    if (s.sticky) {
      const pinEl = document.createElement("span");
      pinEl.className = "session-row__pin";
      pinEl.innerHTML = PIN_SVG;
      pinEl.title = "sticky — restored on backend restart";
      nameWrap.append(pinEl);
    }
    const statusEl = document.createElement("span");
    statusEl.className = "session-row__status"
      + (s.attached ? " session-row__status--attached" : "");
    const winLabel = `${s.windows}w`;
    statusEl.textContent = s.attached
      ? `attached · ${winLabel}`
      : `idle ${relativeTime(s.created)} · ${winLabel}`;

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

    const stickyItem = document.createElement("button");
    stickyItem.type = "button";
    stickyItem.className = "session-row__menu__item";
    stickyItem.textContent = s.sticky ? "make ephemeral" : "make sticky";

    const killItem = document.createElement("button");
    killItem.type = "button";
    killItem.className = "session-row__menu__item danger";
    killItem.textContent = "kill";

    menu.append(renameItem, stickyItem, killItem);
    actions.append(kebab, menu);

    row.append(dot, nameWrap, statusEl, actions);

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
    // Document-level outside-click handling is installed ONCE by the
    // outer renderSessionPicker via _bindOutsideClickClose, not per
    // row — otherwise refresh() each time the user kills a session
    // leaked one document listener per remaining row, growing without
    // bound.

    // ── Rename inline ──
    renameItem.addEventListener("click", (e) => {
      e.stopPropagation();
      closeMenu();
      startRename(row, s, nameEl);
    });

    // ── Sticky toggle ──
    stickyItem.addEventListener("click", async (e) => {
      e.stopPropagation();
      closeMenu();
      const wanted = !s.sticky;
      try {
        const updated = await apiJson<SessionInfo>(
          `/api/sessions/${encodeURIComponent(s.name)}/sticky`,
          { method: "POST", body: JSON.stringify({ sticky: wanted }) },
        );
        // Local mirror so a quick re-open of the menu reflects the new
        // state without waiting for the full refresh() round-trip.
        s.sticky = updated.sticky;
        refresh();
      } catch (err) {
        showError(`sticky toggle failed: ${(err as Error).message}`);
      }
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

  // Single outside-click handler for every row's overflow menu. Closes
  // any open menu when the click target sits outside the row's actions
  // column. Bound once here rather than per-row to avoid leaking
  // listeners across refresh().
  const onOutsideClick = (e: MouseEvent) => {
    const target = e.target as Element | null;
    if (!target) return;
    list.querySelectorAll<HTMLElement>(".session-row__menu:not([hidden])")
      .forEach((m) => {
        const actions = m.parentElement;
        if (!actions || !actions.contains(target)) m.hidden = true;
      });
  };
  document.addEventListener("click", onOutsideClick);
  // Cleanup when the picker is replaced (caller wipes innerHTML; we
  // can't observe that directly, but bootstrap dispatches no event
  // for the picker view today — accept a single listener over the
  // tab's lifetime, which is what we had post-fix and is fine for
  // a long-lived SPA).

  // First render
  renderRecent();
  refresh();
  // Default the cwd input to: a recent dir if we have one, otherwise
  // the fs jail root (operator's home or whatever CCPIPE_FS_ROOT is
  // set to). The previous hardcoded "/home" was annoying — it's the
  // parent of the default jail root, so /api/fs/list 403s on it and
  // the user has to drill in by hand on every fresh session.
  const dirs = loadRecentDirs();
  if (dirs[0]) {
    cwdInput.value = dirs[0];
    void onCwdChanged();
  } else {
    void getFsConfig().then((cfg) => {
      cwdInput.value = cfg.root;
      void onCwdChanged();
    }).catch(() => {
      cwdInput.value = "/";
      void onCwdChanged();
    });
  }
}
