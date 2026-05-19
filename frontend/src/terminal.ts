import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { SearchAddon } from "@xterm/addon-search";
import { WebLinksAddon } from "@xterm/addon-web-links";
import { WebglAddon } from "@xterm/addon-webgl";
import "@xterm/xterm/css/xterm.css";

import {
  DisplayPrefs,
  loadDisplayPrefs,
  saveSessionFontSize,
} from "./display-prefs";
import { isMobileLayout } from "./mobile";
import { resolveTerminalFontFamily } from "./terminal-fonts";
import { TerminalSocket } from "./ws";

/** Pick the terminal-font preference matching the current device.
 * Two prefs are stored — desktop and mobile — because what reads well
 * on a 27" monitor is rarely what reads well on a phone. The mobile
 * default leans on a legibility-tuned bundled font; desktop sticks to
 * the OS native mono. The dropdown UI in settings.ts edits both. */
function pickFontIdForDevice(prefs: DisplayPrefs): string {
  return isMobileLayout() ? prefs.terminalFontMobile : prefs.terminalFontDesktop;
}

/** Promise that resolves when the browser has loaded the primary
 * font in `family` at `size`. Used by applyPrefs to delay the
 * post-font-change resize until xterm can actually measure cells
 * with the NEW font's glyph metrics (resizing while the fallback
 * stack is still being rendered produces miscounted cols/rows that
 * snap to the right values a frame later when the woff2 finishes).
 *
 * Returns immediately if `family` is a bare system stack (no quoted
 * custom font name) or if document.fonts isn't supported. Errors
 * from load() are swallowed — the worst outcome is one extra fit
 * tick when the text reflows on its own. */
function ensureTerminalFontReady(family: string, size: number): Promise<void> {
  if (typeof document === "undefined" || !document.fonts?.load) {
    return Promise.resolve();
  }
  // Take the first family name from the comma-separated stack —
  // that's the custom font we're waiting on. System-stack-only
  // selections start with a bare keyword like `ui-monospace` and
  // need no load wait.
  const primary = family.split(",")[0].trim();
  if (!primary.startsWith("'") && !primary.startsWith('"')) {
    return Promise.resolve();
  }
  return document.fonts.load(`${size}px ${primary}`).then(() => {}, () => {});
}

export function createTerminal(container: HTMLElement, socket: TerminalSocket,
                                initialPrefs: DisplayPrefs = loadDisplayPrefs(),
                                sessionName?: string) {
  // Theme tuned to the ccpipe palette: parchment-on-charcoal with amber
  // accents. The full ANSI 16 are overridden so TUI output (Claude Code uses
  // warm yellow / orange heavily) sits in the same hue family as the chrome.
  const term = new Terminal({
    fontFamily: resolveTerminalFontFamily(pickFontIdForDevice(initialPrefs)),
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
  //
  // The addon instance escapes the try-block scope because
  // applyPrefs() needs to call webgl.clearTextureAtlas() after a font
  // change. xterm's WebGL renderer caches glyphs in a texture atlas
  // keyed on the OLD font; without an explicit clear, post-change
  // renders paint old-shape glyphs at the new cell positions, which
  // looks exactly like "the terminal didn't reflow for the new font".
  let webgl: WebglAddon | null = null;
  let webglActive = false;
  try {
    webgl = new WebglAddon();
    webgl.onContextLoss(() => { webgl?.dispose(); webgl = null; webglActive = false; });
    term.loadAddon(webgl);
    webglActive = true;
  } catch (e) {
    console.warn("xterm webgl renderer unavailable, using DOM:", e);
  }

  // ─── Resize handling ──────────────────────────────────────────────────
  // xterm.js cell metrics depend on the rendered font. If we fit before web
  // fonts swap in we get the wrong cols/rows. We:
  //   1. defer the first fit until document.fonts.ready
  //   2. observe the terminal container with ResizeObserver — the
  //      canonical signal that the actual layout changed. Banner
  //      appearance, composer height, orientation, soft-keyboard,
  //      OS-chrome-overlap CSS var swaps, etc. all flow through here.
  //   3. listen on window.resize for zoom / cross-frame edge cases
  //   4. pre-flight check before fit: if the container is in a
  //      momentary too-small state (page hidden, layout mid-shift,
  //      transient zero-width), DON'T call fit. Calling fit.fit() with
  //      a sub-100px clientWidth propagates a narrow cols to xterm and
  //      the server, and the bug is sticky — when the layout recovers
  //      the post-recovery clientWidth matches the pre-transient
  //      value, ResizeObserver doesn't fire again, and the terminal is
  //      stuck wrapping at ~60 columns until something else nudges
  //      the layout. (Manifested as "browser was inactive, came back
  //      and the terminal is narrow.")
  //   5. NO focus / visibilitychange / pageshow → fit trigger — those
  //      events fire even when no layout change happened, and a
  //      no-op fit can still cause an internal xterm buffer reflow
  //      that briefly puts the viewport at the top of scrollback. If
  //      the OS-chrome-overlap compensation in main.ts changes anything
  //      meaningful, the resulting CSS var swap reflows the layout,
  //      ResizeObserver fires naturally, and we re-fit through the
  //      same path everything else uses.
  //   6. debounce sends; only emit a server resize message when
  //      cols/rows change
  let lastCols = -1;
  let lastRows = -1;
  let pending: number | null = null;
  // Set true by dispose(). Pending scheduleResize timers may fire AFTER
  // dispose if a DOM event landed between the timer clear and the
  // listener removal — checking the flag here prevents fit.fit() /
  // term.cols from running against a disposed terminal.
  let disposed = false;

  // Minimum container size below which we refuse to call fit.fit().
  // A real terminal has dozens of cols/rows; anything below this is a
  // transient layout state we shouldn't propagate. The exact threshold
  // doesn't matter much — anything between ~50px and ~200px catches
  // the bug; pick 100 as a sane "definitely not a real terminal" floor.
  const MIN_CONTAINER_PX = 100;

  const sendResize = () => {
    if (disposed) return;
    // Pre-flight: bail on transient too-small container states. See the
    // numbered comment above for the why.
    if (container.clientWidth < MIN_CONTAINER_PX
        || container.clientHeight < MIN_CONTAINER_PX) return;

    // Snapshot "user is at the live tail" BEFORE fit.fit(). If the
    // cols change, xterm's buffer.resize() reflows the scrollback
    // against the new column count, and the resulting line-index
    // shuffle can leave the viewport pinned at the TOP of the buffer
    // — the user's "I changed font size / clicked away and back and
    // now I'm staring at the oldest scrollback" symptom. We can't
    // suppress xterm's reflow (it has to happen for correct
    // re-wrapping), but we CAN observe whether the user was tailing
    // live output and force them back to the bottom afterwards.
    //
    // Tolerance of 4px in the at-bottom test absorbs DPI rounding /
    // momentary scroll-offset drift around the actual bottom.
    const viewport = container.querySelector(".xterm-viewport") as HTMLElement | null;
    const wasAtBottom = !viewport
      || viewport.scrollTop + viewport.clientHeight
         >= viewport.scrollHeight - 4;

    try {
      fit.fit();
    } catch {
      return;  // container has no size yet (hidden or detached)
    }
    // DOM-renderer safety pad: when WebGL is unavailable, xterm's DOM
    // renderer can report a cell-height slightly smaller than what it
    // actually renders. FitAddon floor()s the rows based on that
    // smaller reported height, so the resulting rows-times-actual-
    // cellHeight overflows the container by half a row and the last
    // row gets clipped at the bottom. Sacrifice one row on the DOM
    // renderer to guarantee no overflow. The WebGL renderer doesn't
    // have this drift and gets the full row count.
    if (!webglActive && term.rows > 2) {
      try { term.resize(term.cols, term.rows - 1); } catch {}
    }
    // Defensive second guard: if fit ever computed a 0/1 cols/rows
    // despite the pre-flight check (it shouldn't, given a 100px+
    // clientWidth, but cell metrics could be wrong for other reasons),
    // don't propagate the bogus size.
    if (term.cols < 2 || term.rows < 2) return;
    const colsRowsChanged =
      term.cols !== lastCols || term.rows !== lastRows;
    if (!colsRowsChanged) return;
    lastCols = term.cols;
    lastRows = term.rows;
    socket.send({ type: "resize", cols: term.cols, rows: term.rows });

    // Restore the at-bottom state. If the user was tailing live output
    // before the fit, snap back to the bottom regardless of what the
    // reflow did. Schedule via rAF so any DOM/render work xterm
    // queued in response to the resize has flushed before we measure
    // and scroll — calling scrollToBottom synchronously here would
    // sometimes run before the buffer reflow had updated scrollHeight.
    if (wasAtBottom) {
      requestAnimationFrame(() => {
        if (disposed) return;
        try { term.scrollToBottom(); } catch {}
      });
    }
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

  // First fit only after fonts have loaded; otherwise cell metrics
  // drift. CRITICAL: just calling sendResize() / fit.fit() here isn't
  // enough when the active font is a custom woff2 — xterm's
  // CharSizeService caches the cell width from whatever font was
  // actually rendered at Terminal-construction time, which is almost
  // always the FALLBACK (the woff2 hasn't downloaded by then). Once
  // fonts.ready fires, the woff2 is loaded but xterm doesn't
  // automatically re-measure, so fit() proposes rows/cols against
  // stale cell metrics and the terminal lays out with extra empty
  // rows at the bottom (= "big space at the bottom" symptom). The
  // fix mirrors applyPrefs's runtime-swap path:
  //
  //   1. clearTextureAtlas() drops the WebGL renderer's glyph cache
  //      (built against the fallback) so the next refresh rasterises
  //      from the now-loaded font.
  //   2. term.refresh() triggers that re-rasterisation and forces
  //      xterm to re-measure cells with the new font.
  //   3. sendResize() picks up the corrected cell metrics.
  //
  // Two fits: once after the refresh, then a 200ms safety pass to
  // mop up late layout shifts (banners, mobile URL-bar transitions,
  // etc.).
  const fontsReady = (document as any).fonts?.ready as Promise<unknown> | undefined;
  if (fontsReady) {
    fontsReady.then(() => {
      if (disposed) return;
      if (webglActive && webgl) {
        try { webgl.clearTextureAtlas(); } catch {}
      }
      try { term.refresh(0, term.rows - 1); } catch {}
      sendResize();
      window.setTimeout(() => {
        if (!disposed) sendResize();
      }, 200);
    });
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

    // Track at-bottom state for the live-pill visibility. We used to
    // also persist scroll offset to localStorage here so a refresh
    // could restore "where the user was reading"; that whole feature
    // was removed in 13b3704 because it kept yanking users out of
    // the live tail. The save side is gone too — nothing reads it.
    const updatePill = () => {
      const atBottom =
        viewport.scrollTop + viewport.clientHeight >= viewport.scrollHeight - 2;
      livePill.hidden = atBottom;
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
   * so cols/rows propagate to the backend.
   *
   * xterm applies font/line-height changes asynchronously — its internal
   * cell-metric measurement happens on the next render pass, not the
   * moment we set the option. A synchronous sendResize() would measure
   * stale cell dimensions and compute the wrong rows/cols. Route
   * through scheduleResize() instead so the debounce + double-rAF
   * pattern lets xterm re-measure first. */
  const applyPrefs = (next: DisplayPrefs): void => {
    // Apply non-font-family options immediately — they don't need
    // a woff2 to be loaded before xterm can measure correctly.
    term.options.fontSize = next.fontSize;
    term.options.lineHeight = next.lineHeight;
    term.options.letterSpacing = next.letterSpacing;
    term.options.cursorBlink = next.cursorBlink;
    term.options.cursorStyle = next.cursorStyle;
    scheduleResize();

    const nextFontFamily = resolveTerminalFontFamily(pickFontIdForDevice(next));
    if (nextFontFamily === term.options.fontFamily) return;

    // Three things need to happen, in this order, for a runtime font
    // change to land cleanly without a page reload:
    //
    //   1. Pre-load the woff2 before assigning to xterm. xterm.js's
    //      CharSizeService runs a SYNCHRONOUS DOM measurement on
    //      assignment and caches the result; if the font isn't yet
    //      loaded at that moment, the measurement reads the fallback
    //      stack and the wrong cell width sticks.
    //   2. Clear the WebGL texture atlas. The WebGL renderer caches
    //      rasterised glyphs in a texture atlas keyed on the OLD
    //      font; without an explicit clear the renderer happily
    //      paints old-shape glyphs even after fontFamily has changed,
    //      which manifests as "the terminal didn't reflow for the
    //      new font" exactly as reported.
    //   3. Request a full refresh and a fit. refresh() re-rasters
    //      every visible row against the now-loaded font; fit() picks
    //      up the new cell metrics and resizes cols/rows + the server.
    //
    // For system fonts (no quoted custom family) step 1 resolves
    // immediately so the user sees no extra latency in the common
    // "I picked System mono" case.
    void ensureTerminalFontReady(nextFontFamily, next.fontSize).then(() => {
      term.options.fontFamily = nextFontFamily;
      if (webglActive && webgl) {
        try { webgl.clearTextureAtlas(); } catch {}
      }
      try { term.refresh(0, term.rows - 1); } catch {}
      scheduleResize();
    });
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
   * top of the buffer.
   *
   * Triple-fire: term.write()'s callback fires when the PARSER has
   * consumed the bytes, but the DOM renderer (used when WebGL is
   * disabled) paints rows in a separate render cycle. Between
   * callback-fires and render-completes, the DOM viewport's
   * scrollHeight hasn't yet caught up to the buffer's row count, so
   * a single term.scrollToBottom() updates xterm's internal ydisp
   * but the visible scroll lands wherever the previous-render DOM
   * scrollTop was. We fire:
   *   1. immediately — xterm's model goes to bottom now,
   *   2. on the next xterm render — DOM has the post-write rows now,
   *      DOM scrollTop catches up,
   *   3. as a safety belt 100ms later — picks up any further async
   *      layout settling that onRender misses (notably the canvas
   *      vs DOM viewport sync on the DOM renderer's slow path).
   */
  const scrollToBottom = (): void => {
    if (disposed) return;
    // Pass 1 — immediate: updates xterm's internal ydisp.
    try { term.scrollToBottom(); } catch {}
    // Pass 2 — after the next render: DOM viewport has the post-write
    // rows by now, so its scrollHeight reflects the buffer. Force the
    // viewport's scrollTop too because the DOM renderer's CSSOM scroll
    // can lag behind xterm's ydisp on the slow path.
    const disposable = term.onRender(() => {
      disposable.dispose();
      if (disposed) return;
      try { term.scrollToBottom(); } catch {}
      const viewport = container.querySelector(".xterm-viewport") as HTMLElement | null;
      if (viewport) viewport.scrollTop = viewport.scrollHeight;
    });
    // Pass 3 — +100ms safety belt: catches any further async layout
    // settling that onRender missed (notably the canvas-vs-viewport
    // sync on the DOM renderer's slow path).
    window.setTimeout(() => {
      if (disposed) return;
      try { term.scrollToBottom(); } catch {}
      const viewport = container.querySelector(".xterm-viewport") as HTMLElement | null;
      if (viewport) viewport.scrollTop = viewport.scrollHeight;
    }, 100);
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
