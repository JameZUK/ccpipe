import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { SearchAddon } from "@xterm/addon-search";
import { WebLinksAddon } from "@xterm/addon-web-links";
import { WebglAddon } from "@xterm/addon-webgl";
import "@xterm/xterm/css/xterm.css";

import {
  DisplayPrefs,
  loadDisplayPrefs,
  saveScrollOffset,
  saveSessionFontSize,
} from "./display-prefs";
import { TerminalSocket } from "./ws";

export function createTerminal(container: HTMLElement, socket: TerminalSocket,
                                initialPrefs: DisplayPrefs = loadDisplayPrefs(),
                                sessionName?: string) {
  // Theme tuned to the ccpipe palette: parchment-on-charcoal with amber
  // accents. The full ANSI 16 are overridden so TUI output (Claude Code uses
  // warm yellow / orange heavily) sits in the same hue family as the chrome.
  const term = new Terminal({
    fontFamily: 'ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace',
    fontSize: initialPrefs.fontSize,
    lineHeight: initialPrefs.lineHeight,
    letterSpacing: initialPrefs.letterSpacing,
    cursorBlink: initialPrefs.cursorBlink,
    cursorStyle: initialPrefs.cursorStyle,
    convertEol: false,
    // Required for term.parser.registerCsiHandler() below — that lives
    // on xterm's "proposed API" surface. We rely on it to suppress
    // alt-screen toggles (?1049h etc.) so TUI content stays in the
    // main buffer where scrollback works. If you ever drop the
    // alt-screen suppression, this flag can come off too. See pass-2
    // review #13 (Info).
    allowProposedApi: true,
    // Generous scrollback so long Claude responses (and replayed tmux
    // history sent on attach) stay reachable by scrolling up.
    scrollback: 10000,
    theme: {
      background: "#0d0c08",
      foreground: "#e8dfc8",
      cursor: "#f5a524",
      cursorAccent: "#0d0c08",
      selectionBackground: "rgba(245, 165, 36, 0.28)",

      black:         "#1d1a14",
      red:           "#e95141",
      green:         "#8eb874",
      yellow:        "#f5a524",
      blue:          "#7c9fc8",
      magenta:       "#c98ec0",
      cyan:          "#7cb0a9",
      white:         "#c8bfa8",

      brightBlack:   "#5a554a",
      brightRed:     "#ee6e60",
      brightGreen:   "#a6cd8b",
      brightYellow:  "#f8bb55",
      brightBlue:    "#9bbadb",
      brightMagenta: "#dfa7d7",
      brightCyan:    "#a0c9c1",
      brightWhite:   "#f0e8d0",
    },
  });

  const fit = new FitAddon();
  term.loadAddon(fit);
  term.loadAddon(new WebLinksAddon());
  const search = new SearchAddon();
  term.loadAddon(search);

  term.open(container);
  // Auto-focus so a freshly-opened session accepts keystrokes immediately
  // without the user having to click the terminal first. On mobile the
  // composer textarea will steal focus right after via mountMobileUI,
  // which is the desired behaviour there.
  term.focus();

  // Suppress alt-screen toggles. tmux's own attach + Claude Code's TUI
  // both send `\x1b[?1049h` (and variants 47, 1047, 1048) which switch
  // xterm to a separate buffer that has no scrollback. By consuming
  // these CSI sequences in the parser, xterm stays on the main screen
  // and every draw lands in the scrollback buffer the user can drag
  // through. registerCsiHandler is under proposed API, which is already
  // enabled above.
  const ALT_SCREEN_MODES = new Set([47, 1047, 1048, 1049]);
  const suppressIfAltScreen = (params: (number | number[])[]): boolean => {
    const first = Array.isArray(params[0]) ? params[0][0] : params[0];
    return typeof first === "number" && ALT_SCREEN_MODES.has(first);
  };
  term.parser.registerCsiHandler({ prefix: "?", final: "h" }, suppressIfAltScreen);
  term.parser.registerCsiHandler({ prefix: "?", final: "l" }, suppressIfAltScreen);

  // Upgrade to the WebGL renderer when available. 2-5x faster on Claude
  // Code's busy TUI redraws; falls back silently to the default DOM
  // renderer if the addon fails to attach (no WebGL2, context lost, etc.).
  try {
    const webgl = new WebglAddon();
    webgl.onContextLoss(() => webgl.dispose());
    term.loadAddon(webgl);
  } catch (e) {
    console.warn("xterm webgl renderer unavailable, using DOM:", e);
  }

  // ─── Resize handling ──────────────────────────────────────────────────
  // xterm.js cell metrics depend on the rendered font. If we fit before web
  // fonts swap in we get the wrong cols/rows. We:
  //   1. defer the first fit until document.fonts.ready
  //   2. observe the terminal container with ResizeObserver (catches banner
  //      appearance, composer height, orientation, soft-keyboard, etc.)
  //   3. also listen on window.resize for zoom / cross-frame edge cases
  //   4. debounce sends; only emit a resize message when cols/rows change
  let lastCols = -1;
  let lastRows = -1;
  let pending: number | null = null;
  // Set true by dispose(). Pending scheduleResize timers may fire AFTER
  // dispose if a DOM event landed between the timer clear and the
  // listener removal — checking the flag here prevents fit.fit() /
  // term.cols from running against a disposed terminal.
  let disposed = false;

  const sendResize = () => {
    if (disposed) return;
    try {
      fit.fit();
    } catch {
      return;  // container has no size yet (hidden or detached)
    }
    if (term.cols === lastCols && term.rows === lastRows) return;
    lastCols = term.cols;
    lastRows = term.rows;
    socket.send({ type: "resize", cols: term.cols, rows: term.rows });
  };

  const scheduleResize = () => {
    if (disposed) return;
    if (pending !== null) clearTimeout(pending);
    pending = window.setTimeout(() => {
      pending = null;
      sendResize();
    }, 60);
  };

  const ro = new ResizeObserver(scheduleResize);
  ro.observe(container);
  window.addEventListener("resize", scheduleResize);
  window.addEventListener("orientationchange", scheduleResize);
  // visualViewport changes when the soft keyboard opens / closes on mobile
  const vv = (window as any).visualViewport as VisualViewport | undefined;
  vv?.addEventListener("resize", scheduleResize);

  // First fit only after fonts have loaded; otherwise cell metrics drift.
  const fontsReady = (document as any).fonts?.ready as Promise<unknown> | undefined;
  if (fontsReady) {
    fontsReady.then(sendResize);
  } else {
    requestAnimationFrame(sendResize);
  }

  // PTY output → terminal. xterm.js accepts string OR Uint8Array; bytes
  // skip a decode/encode roundtrip and avoid split-codepoint corruption.
  //
  // The optional ``after`` callback is forwarded to xterm.js's own
  // post-process hook on term.write(). Critical for the pane-replay
  // path: term.write() is asynchronous (data is queued and processed
  // via an internal microtask loop), so anything that needs to happen
  // AFTER the buffer has actually grown — like scrolling to the
  // bottom of the just-replayed scrollback — must run inside this
  // callback, not synchronously after the write() call returns.
  const writeToTerm = (data: Uint8Array | string, after?: () => void) => {
    if (after) {
      term.write(data, after);
    } else {
      term.write(data);
    }
  };

  // Terminal input → PTY. Most data is small (single keystrokes), so
  // ship it through directly. Pastes (xterm's onData fires once with
  // the whole pasted blob, even with bracketed-paste mode wrapping)
  // can be many KB — splitting into 4 KB chunks with a microtask
  // yield between them gives the server's PTY drain time to push each
  // through before the next arrives, instead of slamming the PTY
  // master with one giant write that exceeds the kernel buffer and
  // backpressures the receive loop.
  const INPUT_CHUNK = 4096;
  term.onData((data) => {
    if (data.length <= INPUT_CHUNK) {
      socket.send({ type: "input", data });
      return;
    }
    let i = 0;
    const next = () => {
      if (i >= data.length) return;
      const slice = data.slice(i, i + INPUT_CHUNK);
      i += INPUT_CHUNK;
      socket.send({ type: "input", data: slice });
      // setTimeout(0) yields to the event loop so the WS send actually
      // flushes (and the server gets to process each chunk) instead of
      // queueing everything in a microtask burst.
      setTimeout(next, 0);
    };
    next();
  });

  // ─── Search overlay ───────────────────────────────────────────────────
  // Cmd/Ctrl+F opens a small input that wraps the SearchAddon. n/N goes
  // forward / backward, Esc closes. The match highlight is the addon's
  // built-in selection style.
  const searchBar = document.createElement("form");
  searchBar.className = "term-search";
  searchBar.hidden = true;
  searchBar.innerHTML = `
    <input type="search" placeholder="search…" spellcheck="false" autocapitalize="none" autocomplete="off" />
    <button type="submit" class="term-search__btn" title="Next (Enter)">↓</button>
    <button type="button" class="term-search__btn" data-prev title="Previous (Shift+Enter)">↑</button>
    <button type="button" class="term-search__btn" data-close title="Close (Esc)">×</button>
  `;
  container.appendChild(searchBar);
  const searchInput = searchBar.querySelector<HTMLInputElement>("input")!;
  const closeSearch = () => {
    searchBar.hidden = true;
    try { search.clearDecorations(); } catch {}
    term.focus();
  };
  const findNext = (back = false) => {
    const q = searchInput.value;
    if (!q) return;
    const opts = { caseSensitive: false, wholeWord: false, regex: false };
    if (back) search.findPrevious(q, opts);
    else search.findNext(q, opts);
  };
  searchBar.addEventListener("submit", (e) => {
    e.preventDefault();
    findNext(false);
  });
  searchBar.querySelector("[data-prev]")!.addEventListener("click", () => findNext(true));
  searchBar.querySelector("[data-close]")!.addEventListener("click", closeSearch);
  searchInput.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.preventDefault(); closeSearch(); }
    else if (e.key === "Enter" && e.shiftKey) { e.preventDefault(); findNext(true); }
  });
  // Per-session font-size shortcuts. Ctrl/Cmd + adjusts by 1px each
  // step, persisted under the session key so reopening the same
  // session restores it. Range matches the picker font slider's bounds.
  const adjustFontSize = (delta: number | "reset") => {
    const cur = term.options.fontSize ?? initialPrefs.fontSize;
    const next = delta === "reset"
      ? loadDisplayPrefs().fontSize       // global pref, ignoring session override
      : Math.max(8, Math.min(32, cur + delta));
    if (next === cur) return;
    term.options.fontSize = next;
    if (sessionName) saveSessionFontSize(sessionName, next);
    sendResize();
  };

  const onTermKeyForSearch = (e: KeyboardEvent) => {
    const cmdLike = e.ctrlKey || e.metaKey;
    if (cmdLike && e.key.toLowerCase() === "f") {
      e.preventDefault();
      searchBar.hidden = false;
      searchInput.focus();
      searchInput.select();
      return;
    }
    if (cmdLike && (e.key === "=" || e.key === "+")) {
      e.preventDefault();
      adjustFontSize(+1);
      return;
    }
    if (cmdLike && (e.key === "-" || e.key === "_")) {
      e.preventDefault();
      adjustFontSize(-1);
      return;
    }
    if (cmdLike && e.key === "0") {
      e.preventDefault();
      adjustFontSize("reset");
      return;
    }
  };
  // Bind on the document so it works even when xterm has focus (xterm
  // captures most keys but Ctrl+F isn't part of its grab list).
  document.addEventListener("keydown", onTermKeyForSearch);

  // ─── "↓ live" pill ────────────────────────────────────────────────────
  // Appears when the user has scrolled away from the live tail; tapping
  // jumps back to the bottom. The xterm container needs position:relative
  // for the pill to anchor (set in CSS on #terminal).
  const livePill = document.createElement("button");
  livePill.type = "button";
  livePill.className = "live-pill";
  livePill.hidden = true;
  livePill.setAttribute("aria-label", "Scroll to live");
  livePill.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 5v14"/><polyline points="6 13 12 19 18 13"/></svg><span>live</span>`;
  container.appendChild(livePill);
  livePill.addEventListener("click", () => term.scrollToBottom());

  // Hook the xterm viewport for live-pill state, and bind touch-to-scroll
  // via PointerEvents on the outer container in capture phase.
  //
  // Why so heavy-handed: xterm v5 + the WebGL renderer's canvas layers
  // absorb touches before they reach .xterm-viewport. CSS touch-action
  // doesn't help once xterm internally calls preventDefault on touchmove.
  // The only reliable approach is to catch PointerEvents on the OUTER
  // #terminal element in capture phase (which fires before any
  // descendant handler) and drive viewport.scrollTop ourselves.
  let pillCleanup: (() => void) | null = null;
  let scrollCleanup: (() => void) | null = null;

  const wireScrollAffordances = () => {
    const viewport = container.querySelector(".xterm-viewport") as HTMLElement | null;
    if (!viewport) { setTimeout(wireScrollAffordances, 40); return; }

    // Persist scroll position per session so a refresh lands the user
    // roughly where they were reading. We store the offset-from-bottom
    // (in pixels) rather than absolute scrollTop, because the buffer
    // grows asynchronously after attach.
    let scrollSaveTimer: number | null = null;
    const updatePill = () => {
      const atBottom =
        viewport.scrollTop + viewport.clientHeight >= viewport.scrollHeight - 2;
      livePill.hidden = atBottom;
      if (sessionName) {
        const offset = Math.max(
          0, viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight,
        );
        if (scrollSaveTimer !== null) clearTimeout(scrollSaveTimer);
        scrollSaveTimer = window.setTimeout(
          () => saveScrollOffset(sessionName, offset), 200);
      }
    };
    viewport.addEventListener("scroll", updatePill, { passive: true });
    term.onScroll(updatePill);
    pillCleanup = () => viewport.removeEventListener("scroll", updatePill);
    updatePill();

    // (Removed) The 1200ms savedOffset restoration timer that used to
    // try to put the viewport back where the user was last reading
    // before a reload. In practice it failed in two unfortunate ways:
    //   - when the user had been at the bottom (live tail) it was a
    //     no-op (saved=0), so it never helped in the common case;
    //   - when the user HAD scrolled up, it yanked them BACK to that
    //     spot after a reload, which they almost always perceive as
    //     "ccpipe just dropped me out of the live tail" — exactly the
    //     symptom that motivated removing it.
    // Live tail is now the unconditional behaviour after attach/replay.
    // The user can still scroll up to read; the live-pill button takes
    // them back when they want.

    // PointerEvents in capture phase. Pointer Events unify touch + mouse
    // and are dispatched even when xterm has bound listeners deeper in
    // the tree. Capture-phase means we get them BEFORE xterm's handlers.
    let activeId = -1;
    let lastY = 0;
    let dragging = false;
    const DRAG_THRESHOLD_PX = 4;

    const onDown = (e: PointerEvent) => {
      if (e.pointerType !== "touch") return;
      if (activeId !== -1) return;
      activeId = e.pointerId;
      lastY = e.clientY;
      dragging = false;
    };
    const onMove = (e: PointerEvent) => {
      if (e.pointerId !== activeId) return;
      const dy = lastY - e.clientY;
      if (!dragging && Math.abs(dy) < DRAG_THRESHOLD_PX) return;
      dragging = true;
      viewport.scrollTop += dy;
      lastY = e.clientY;
      if (e.cancelable) e.preventDefault();
    };
    const onUp = (e: PointerEvent) => {
      if (e.pointerId === activeId) { activeId = -1; dragging = false; }
    };

    container.addEventListener("pointerdown",   onDown, { capture: true });
    container.addEventListener("pointermove",   onMove, { capture: true });
    container.addEventListener("pointerup",     onUp,   { capture: true });
    container.addEventListener("pointercancel", onUp,   { capture: true });
    scrollCleanup = () => {
      container.removeEventListener("pointerdown",   onDown, true);
      container.removeEventListener("pointermove",   onMove, true);
      container.removeEventListener("pointerup",     onUp,   true);
      container.removeEventListener("pointercancel", onUp,   true);
    };
  };
  wireScrollAffordances();

  /** Apply new display prefs live; cell-metric changes trigger a re-fit
   * so cols/rows propagate to the backend. */
  const applyPrefs = (next: DisplayPrefs): void => {
    term.options.fontSize = next.fontSize;
    term.options.lineHeight = next.lineHeight;
    term.options.letterSpacing = next.letterSpacing;
    term.options.cursorBlink = next.cursorBlink;
    term.options.cursorStyle = next.cursorStyle;
    // Force a re-fit because cell dimensions changed.
    sendResize();
  };

  /** Clear scrollback + visible AND reset terminal state so the next
   * write starts from a clean slate. Used on every WS hello so the
   * backend's pane-replay replaces xterm's buffer rather than appending.
   * Without this, content that scrolled out of view during a network
   * blip never made it into xterm's scrollback.
   *
   * The trailing scrollToBottom() is belt-and-braces — it sets xterm's
   * internal "user at bottom" flag while the buffer is still empty,
   * which puts auto-follow in the right state for the very first
   * chunk that arrives. The LOAD-BEARING fix lives in main.ts onOutput
   * which scrolls via term.write()'s completion callback after the
   * history bytes have actually been processed; this scroll alone is
   * not enough because term.write() is asynchronous and a synchronous
   * scrollToBottom() after it would run before the buffer has grown. */
  const resetBuffer = (): void => {
    if (disposed) return;
    try { term.reset(); } catch {}
    try { term.scrollToBottom(); } catch {}
  };

  /** Force the viewport to the bottom. Called from the term.write()
   * completion callback in main.ts onOutput, so it runs AFTER xterm
   * has processed the pane-replay bytes — that ordering is what makes
   * the scroll actually land at the live tail rather than at the
   * top of the buffer. */
  const scrollToBottom = (): void => {
    if (disposed) return;
    try { term.scrollToBottom(); } catch {}
  };

  return {
    term, writeToTerm, sendResize, applyPrefs, resetBuffer, scrollToBottom,
    dispose: () => {
      disposed = true;
      pillCleanup?.();
      scrollCleanup?.();
      if (pending !== null) { clearTimeout(pending); pending = null; }
      try { ro.disconnect(); } catch {}
      window.removeEventListener("resize", scheduleResize);
      window.removeEventListener("orientationchange", scheduleResize);
      vv?.removeEventListener("resize", scheduleResize);
      document.removeEventListener("keydown", onTermKeyForSearch);
      try { term.dispose(); } catch {}
    },
  };
}
