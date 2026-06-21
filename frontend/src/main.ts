import "./styles.css";
import { fetchAuthStatus, isSecureContext, renderLogin } from "./auth";
import {
  clearLastSession,
  loadDisplayPrefs,
  loadLastSession,
  onDisplayPrefsChange,
  saveLastSession,
} from "./display-prefs";
import { DOC_SVG, FOLDER_SVG, GEAR_SVG, MIC_SVG, TTS_MUTED_SVG, TTS_SVG } from "./icons";
import { isMobileLayout, mountMobileUI } from "./mobile";
import * as notifications from "./notifications";
import { attachOptionSpacePtt } from "./ptt";
import { TerminalSocket } from "./ws";
// Heavy chunks loaded lazily on user gesture:
//   - settings / file-panel / session-picker are large (758 + 752 + 629
//     LOC) and gated behind a button or a no-last-session bootstrap.
//   - xterm, mic, tts, waveform are loaded when a terminal view attaches.
import { getMicConfig, type MicConfig } from "./api";
import type { MicStreamer as MicStreamerType } from "./mic";
import type { TtsPlayer as TtsPlayerType } from "./tts";
import * as wakeLock from "./wake-lock";

const app = document.getElementById("app")!;

// The service worker registration was a no-op (sw.js fetch handler
// was empty); dropping the registration saves a network round-trip
// on every cold load. If we ever want offline support / hashed
// asset caching, this is where it'd come back — for now ccpipe is
// online-only by design.
//
// Existing installs still have the SW registered. The next cache
// purge (or eight days of inactivity → CacheStorage eviction) will
// clean it up; nothing in the SW intercepts fetches so there's no
// staleness risk in the meantime.

// ─── OS-chrome (taskbar) compensation ────────────────────────────────────
// When the browser window extends behind an OS taskbar — e.g. fullscreen
// mode (F11), borderless-maximised tiling, or kiosk-style window
// managers — the browser viewport's `window.innerHeight` includes the
// pixel band covered by the taskbar. Our layout chains `height: 100%`
// from html → body → #app → .terminal-view → #terminal, so #terminal
// ends up rendering its bottom rows into the area visually occluded by
// the taskbar and the user sees only the top half of the last row.
//
// The signal we need is the OS-reported work area height
// (`screen.availHeight`). If `window.innerHeight > screen.availHeight`,
// the difference is exactly the amount of overlap. We expose it as a
// CSS custom property and have `body` use
// `height: calc(100% - var(--os-chrome-overlap, 0px))` to shrink the
// layout above the taskbar.
//
// Triggers: resize (window size changes), focus / pageshow /
// visibilitychange (the user may have toggled fullscreen / moved the
// window between monitors with different work areas while we weren't
// looking). None of these directly trigger an xterm fit — they update
// the CSS variable, which only fires a real layout change when the
// overlap actually moves. ResizeObserver on #terminal picks that up
// and re-fits through the normal path.
function applyOsChromeCompensation(): void {
  let overlap = 0;
  try {
    const avail = screen.availHeight;
    if (typeof avail === "number" && avail > 0) {
      overlap = Math.max(0, window.innerHeight - avail);
    }
  } catch { /* screen unavailable in this environment */ }
  // Ignore sub-row noise (<8px, almost certainly DPI rounding) AND
  // implausibly large values (>200px, almost certainly a misdetection
  // on a multi-monitor or misbehaving-compositor setup where
  // screen.availHeight reports one monitor and window.innerHeight
  // spans multiple). A real OS taskbar fits between ~24 and ~80 px.
  if (overlap < 8 || overlap > 200) overlap = 0;
  document.documentElement.style.setProperty("--os-chrome-overlap", `${overlap}px`);
}
window.addEventListener("resize", applyOsChromeCompensation);
window.addEventListener("focus", applyOsChromeCompensation);
window.addEventListener("pageshow", applyOsChromeCompensation);
document.addEventListener("visibilitychange", applyOsChromeCompensation);
applyOsChromeCompensation();

// PWA share_target: when the user shares text/URL/title from another
// app into ccpipe, the launch URL is /?text=…&url=…&title=…. We snag
// those values once into sessionStorage so the composer's onmount path
// can offer them — but we now ASK before pasting rather than dropping
// arbitrary text straight into the prompt (the composer feeds a shell).
//
// Two deliberate changes from the pre-fix version:
//   1. We DO NOT call history.replaceState() any more. The URL keeps
//      its query params so the operator can see exactly where the text
//      came from before they accept it. Silently scrubbing the URL was
//      what made the previous behaviour a usable social-engineering
//      vector (e.g. a Slack link preview that would silently pre-fill
//      a destructive command).
//   2. The consumer must call ``commitPendingShare()`` to actually take
//      the text — peek + commit are separate, so a caller can render
//      a review prompt first.
function _capturePendingShare(): void {
  try {
    const params = new URLSearchParams(location.search);
    const parts: string[] = [];
    for (const key of ["title", "text", "url"]) {
      const v = params.get(key);
      if (v) parts.push(v);
    }
    if (parts.length === 0) return;
    sessionStorage.setItem("ccpipe.pendingShare", parts.join("\n"));
    // Intentionally NOT calling history.replaceState() — see comment.
  } catch {}
}
_capturePendingShare();

/** Peek at pending shared text without consuming it. */
export function peekPendingShare(): string | null {
  try {
    return sessionStorage.getItem("ccpipe.pendingShare");
  } catch { return null; }
}

/** Drop any pending shared text without inserting it (user dismissed). */
export function discardPendingShare(): void {
  try { sessionStorage.removeItem("ccpipe.pendingShare"); } catch {}
}

/** Read the pending shared text and clear it. Use only after the user
 * has explicitly opted in to inserting it into the composer. */
export function commitPendingShare(): string | null {
  try {
    const v = sessionStorage.getItem("ccpipe.pendingShare");
    if (v) sessionStorage.removeItem("ccpipe.pendingShare");
    return v;
  } catch { return null; }
}

/** Deprecated alias retained so any cached frontend bundle that still
 * imports the old name doesn't break. New code should use peek/commit
 * via the review-chip flow. */
export function consumePendingShare(): string | null {
  return commitPendingShare();
}

function wsUrlFor(session: string): string {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  // No more skip_history on reconnect — the server now always replays
  // tmux's full pane and the frontend `term.reset()`s on hello so the
  // replay replaces rather than appends. This is what fixes the bug
  // where output that arrived during a disconnect window never made it
  // into xterm's scrollback (refresh was the only workaround).
  return `${proto}//${location.host}/ws?session=${encodeURIComponent(session)}`;
}

let authRequired = false;

// ─── Terminal view ──────────────────────────────────────────────────────

async function attachTerminal(session: string): Promise<void> {
  // Remember it so a refresh / reconnect on flaky network lands back in
  // the same session instead of bouncing to the picker.
  saveLastSession(session);

  // Fire dispose on whatever is currently mounted (session picker or a
  // prior terminal view) before wiping it, so its timers/listeners
  // (e.g. the picker's health-poll interval + idle callback) tear down
  // promptly rather than lingering until a fallback isConnected check.
  // Clearing via innerHTML alone fires no events.
  for (const el of app.querySelectorAll(".frame, .terminal-view")) {
    el.dispatchEvent(new CustomEvent("ccpipe:dispose"));
  }
  app.innerHTML = "";
  const mobile = isMobileLayout();
  document.body.classList.toggle("mobile", mobile);

  const [{ createTerminal }, { MicStreamer }, { TtsPlayer }, { Waveform }] =
    await Promise.all([
      import("./terminal"),
      import("./mic"),
      import("./tts"),
      import("./waveform"),
    ]);

  const view = document.createElement("div");
  view.className = "terminal-view";

  // ─── Status bar ───────────────────────────────────────────────────────
  const statusbar = document.createElement("div");
  statusbar.className = "statusbar";

  const brand = document.createElement("button");
  brand.className = "statusbar__brand";
  brand.title = "Back to sessions";
  brand.innerHTML = `<span class="wordmark small">cc<span class="dot"></span>pipe</span>`;
  brand.onclick = () => {
    // Manual return to picker — explicitly clear lastSession so a
    // subsequent refresh doesn't bounce back into the current session.
    // Close the WS first so its reconnect loop doesn't survive the
    // re-render and quietly hold an authenticated connection open.
    clearLastSession();
    socket.close();
    bootstrap();
  };

  const divider1 = document.createElement("div");
  divider1.className = "statusbar__divider";

  const dot = document.createElement("div");
  dot.className = "statusbar__dot";

  const stateLabel = document.createElement("div");
  stateLabel.className = "statusbar__state";
  stateLabel.textContent = "connecting";

  // Latency pip — sits next to the connection dot, shows the most
  // recent ping→pong RTT. Colour-coded so a glance tells you whether
  // the connection is comfortable, draggy, or about to wobble.
  const latencyLabel = document.createElement("div");
  latencyLabel.className = "statusbar__latency";
  latencyLabel.title = "Most recent ping round-trip";
  latencyLabel.textContent = "";
  latencyLabel.hidden = true;

  const sessionLabel = document.createElement("div");
  sessionLabel.className = "statusbar__session";
  // DOM-construct rather than escapeHtml + innerHTML — textContent
  // can't interpolate markup, so a future maintainer can't drop the
  // escape by accident when copying this snippet around.
  renderSessionLabel(sessionLabel, session);

  const controls = document.createElement("div");
  controls.className = "statusbar__controls";

  const ttsWaveCanvas = document.createElement("canvas");
  ttsWaveCanvas.className = "tts-wave";
  ttsWaveCanvas.hidden = true;
  ttsWaveCanvas.title = "TTS playback indicator";

  const ttsBtn = document.createElement("button");
  ttsBtn.className = "pill";
  ttsBtn.dataset.role = "tts";
  ttsBtn.title = "Toggle voice output";
  ttsBtn.setAttribute("aria-pressed", "true");
  // Default visible — TTS is essentially always wired up in this app, and
  // before the global [hidden]{display:none!important} rule landed, the
  // `.pill { display: inline-flex }` rule was masking a bug here: the
  // initial hidden=true had no effect, so the button always showed. Now
  // that hidden actually hides, only flip to true if onHello explicitly
  // tells us the server has TTS disabled.

  // Replay pill — re-speaks the last assistant turn on demand. Hidden
  // until at least one TTS utterance has been seen this session.
  const replayBtn = document.createElement("button");
  replayBtn.className = "pill pill--icon";
  replayBtn.dataset.role = "replay";
  replayBtn.title = "Repeat last response";
  replayBtn.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/></svg>`;
  replayBtn.hidden = true;
  const updateReplayBtn = () => {
    replayBtn.hidden = !lastSpokenText;
  };
  replayBtn.addEventListener("click", () => {
    if (!lastSpokenText) return;
    void tts.playText(lastSpokenText);
  });

  // Docs — a dropdown of every Markdown file under the session's project
  // root; picking one opens the rendered viewer (/view) in a new tab.
  const docsBtn = document.createElement("button");
  docsBtn.className = "pill pill--icon";
  docsBtn.title = "Project docs (Markdown)";
  docsBtn.innerHTML = DOC_SVG;
  let docsMenu: HTMLElement | null = null;
  const closeDocsMenu = () => {
    docsMenu?.remove();
    docsMenu = null;
    document.removeEventListener("pointerdown", onDocsAway, true);
  };
  function onDocsAway(e: Event) {
    const t = e.target as Node;
    if (docsMenu && !docsMenu.contains(t) && !docsBtn.contains(t)) closeDocsMenu();
  }
  docsBtn.addEventListener("click", async (e) => {
    e.stopPropagation();
    if (docsMenu) { closeDocsMenu(); return; }
    const menu = document.createElement("div");
    menu.className = "docs-menu";
    const r = docsBtn.getBoundingClientRect();
    menu.style.top = `${Math.round(r.bottom + 6)}px`;
    menu.style.right = `${Math.round(window.innerWidth - r.right)}px`;
    const loading = document.createElement("div");
    loading.className = "docs-menu__note";
    loading.textContent = "Loading…";
    menu.append(loading);
    document.body.append(menu);
    docsMenu = menu;
    document.addEventListener("pointerdown", onDocsAway, true);
    // Once populated the menu may be wider than the space left of its
    // anchor button (overflows off-screen on narrow phones), so flip it
    // to left-anchored if its left edge spills past the viewport edge.
    const clampMenu = () => {
      const m = menu.getBoundingClientRect();
      if (m.left < 8) { menu.style.right = "auto"; menu.style.left = "8px"; }
      else if (m.right > window.innerWidth - 8) { menu.style.right = "8px"; }
    };
    try {
      if (!sessionCwd) {
        loading.textContent = "No project directory yet.";
        return;
      }
      const root = sessionCwd;
      const { listMarkdown } = await import("./api");
      const data = await listMarkdown(root);
      if (docsMenu !== menu) return;   // closed/reopened while loading
      menu.replaceChildren();
      if (!data.entries.length) {
        const empty = document.createElement("div");
        empty.className = "docs-menu__note";
        empty.textContent = "No Markdown files found.";
        menu.append(empty);
        clampMenu();
        return;
      }
      for (const ent of data.entries) {
        const item = document.createElement("button");
        item.type = "button";
        item.className = "docs-menu__item";
        item.textContent = ent.rel;
        item.title = ent.rel;
        item.addEventListener("click", () => {
          closeDocsMenu();
          window.open(
            `/view?path=${encodeURIComponent(ent.path)}&root=${encodeURIComponent(root)}`,
            "_blank", "noopener");
        });
        menu.append(item);
      }
      if (data.truncated) {
        const note = document.createElement("div");
        note.className = "docs-menu__note";
        note.textContent = `first ${data.entries.length} shown`;
        menu.append(note);
      }
      clampMenu();
    } catch {
      if (docsMenu === menu) loading.textContent = "Failed to load docs.";
    }
  });

  // File panel — opens the upload/download/edit sheet rooted at the
  // session's cwd if we can resolve it, otherwise /home.
  const filesBtn = document.createElement("button");
  filesBtn.className = "pill pill--icon";
  filesBtn.title = "Files";
  filesBtn.innerHTML = FOLDER_SVG;
  filesBtn.addEventListener("click", async () => {
    // Prefer the session's working directory (the project root the
    // user is actively in) so the panel opens where they're working
    // rather than at the fs jail root. Falls back to the fs root if
    // hello hasn't arrived yet or the server couldn't resolve it.
    const { getFsConfig } = await import("./api");
    const [{ openFilePanel }, cfg] = await Promise.all([
      import("./file-panel"),
      getFsConfig().catch(() => ({ root: "/" } as { root: string })),
    ]);
    openFilePanel(document.body, { initialPath: sessionCwd ?? cfg.root });
  });

  const settingsBtn = document.createElement("button");
  settingsBtn.className = "pill pill--icon";
  settingsBtn.title = "Settings";
  settingsBtn.innerHTML = GEAR_SVG;
  settingsBtn.onclick = async () => {
    const { openSettings } = await import("./settings");
    openSettings({
      authRequired,
      onDisplayPrefsChange: (p) => terminalApi?.applyPrefs(p),
      onSessionInvalidated: () => { socket.close(); bootstrap(); },
      onMicConfigChange: (cfg) => { applyMicConfig(cfg); },
      onCaptureDebugSnapshot: () => {
        if (!terminalApi) return;
        // Lazy import keeps the debug module out of the main bundle —
        // it's only loaded when the user actually opens the affordance.
        void import("./debug").then(({ captureSnapshot, showDebugModal }) => {
          const snap = captureSnapshot({ session, terminal: terminalApi, socket });
          showDebugModal(snap);
        });
      },
    });
  };

  // Desktop mic lives in the statusbar between TTS and Files. Mobile
  // uses the composer mic in mobile.ts; on desktop the floating FAB
  // was visually disconnected from the rest of the toolbar, so we
  // surface it here as a sibling pill matching the TTS button shape.
  const micBtn = document.createElement("button");
  micBtn.className = "pill pill--icon pill--mic";
  micBtn.dataset.role = "mic";
  micBtn.title = "Tap to start dictation, tap again to stop. Or hold Option+Space.";
  micBtn.innerHTML = MIC_SVG;
  micBtn.hidden = true;

  controls.append(ttsWaveCanvas, replayBtn, ttsBtn, micBtn, docsBtn, filesBtn, settingsBtn);
  statusbar.append(brand, divider1, dot, stateLabel, latencyLabel, sessionLabel, controls);

  // ─── Terminal ─────────────────────────────────────────────────────────
  const terminalContainer = document.createElement("div");
  terminalContainer.id = "terminal";

  view.append(statusbar, terminalContainer);
  app.append(view);

  // Mic button (desktop only — mobile uses the composer mic). Constructed
  // earlier so it lives in the statusbar; alias for the rest of this
  // function which still uses the old name.
  const micFab = micBtn;

  // ─── Connection-status subscribers ────────────────────────────────────
  // Several UI bits (mobile composer, offline banner) need to know
  // whether the WS is currently open. We fan out via this pub-sub so
  // each can subscribe + receive an immediate cb with the current state.
  let lastConnected = false;
  const connSubs: Array<(c: boolean) => void> = [];
  const setConnected = (c: boolean): void => {
    lastConnected = c;
    for (const cb of connSubs) {
      try { cb(c); } catch (e) { console.warn("conn sub failed:", e); }
    }
  };
  const onConnectionChange = (cb: (c: boolean) => void): (() => void) => {
    connSubs.push(cb);
    cb(lastConnected);
    return () => {
      const idx = connSubs.indexOf(cb);
      if (idx >= 0) connSubs.splice(idx, 1);
    };
  };

  // ─── Offline banner ──────────────────────────────────────────────────
  // Appears when reconnecting for > 10s. Shows "retrying in Ns" countdown
  // and a manual "Retry now" button that resets backoff.
  let offlineBanner: HTMLDivElement | null = null;
  let offlineTimer: number | null = null;
  let bannerCountdownTimer: number | null = null;
  let lastRetryInfo: { attempt: number; nextRetryMs?: number } | null = null;
  // Wall-clock deadline at which the next retry is scheduled to fire.
  // The interval ticks every second and computes remaining = deadline -
  // now, so the banner actually counts down rather than displaying the
  // same nextRetryMs every tick until the next status event arrives.
  let retryDeadlineMs = 0;

  const clearOfflineTimers = () => {
    if (offlineTimer !== null) { clearTimeout(offlineTimer); offlineTimer = null; }
    if (bannerCountdownTimer !== null) {
      clearInterval(bannerCountdownTimer);
      bannerCountdownTimer = null;
    }
  };

  const hideOfflineBanner = () => {
    clearOfflineTimers();
    retryDeadlineMs = 0;
    if (offlineBanner) {
      offlineBanner.remove();
      offlineBanner = null;
    }
  };

  const renderOfflineBanner = () => {
    if (!offlineBanner) {
      offlineBanner = document.createElement("div");
      offlineBanner.className = "banner banner--offline";
      offlineBanner.innerHTML = `
        <div class="banner__icon">⌬</div>
        <div class="banner__body">
          <strong>offline</strong>
          <div data-role="msg">trying to reconnect…</div>
        </div>
        <button class="btn btn--ghost btn--icon" data-role="retry">retry now</button>
      `;
      offlineBanner.querySelector<HTMLButtonElement>("[data-role=retry]")!
        .addEventListener("click", () => socket.reconnectNow(true));
      view.insertBefore(offlineBanner, terminalContainer);
    }
    const msg = offlineBanner.querySelector<HTMLDivElement>("[data-role=msg]")!;
    if (retryDeadlineMs > 0) {
      const remainingMs = Math.max(0, retryDeadlineMs - Date.now());
      const s = Math.ceil(remainingMs / 1000);
      msg.textContent = `retrying in ${s}s (attempt ${lastRetryInfo?.attempt ?? "?"})`;
    } else if (lastRetryInfo?.attempt) {
      msg.textContent = `retrying… (attempt ${lastRetryInfo.attempt})`;
    } else {
      msg.textContent = `trying to reconnect…`;
    }
  };

  // HTTPS warning shown when the server has the pipe but the browser
  // is in plain-HTTP mode and getUserMedia will refuse.
  let httpsBanner: HTMLDivElement | null = null;
  const showHttpsBanner = () => {
    if (httpsBanner) return;
    httpsBanner = document.createElement("div");
    httpsBanner.className = "banner banner--warn";
    httpsBanner.innerHTML = `
      <div class="banner__icon">!</div>
      <div class="banner__body">
        <strong>voice needs https</strong>
        <div>this page is plain http; browsers won't grant mic access. text + tts playback still work.</div>
      </div>
      <button class="banner__close" aria-label="dismiss">&#x2715;</button>
    `;
    httpsBanner.querySelector(".banner__close")?.addEventListener("click", () => {
      httpsBanner?.remove();
      httpsBanner = null;
    });
    view.insertBefore(httpsBanner, terminalContainer);
  };

  let writeToTerm:
    | ((d: Uint8Array | string, after?: () => void) => void)
    | null = null;
  // Set once createTerminal has run. Called from onHello so each
  // (re)connect starts with a clean buffer before the server's pane
  // replay writes the current scrollback into it.
  let resetTerminal: (() => void) | null = null;
  let scrollTerminalToBottom: (() => void) | null = null;
  // Flag set by onHello, cleared on the next onOutput. After the post-
  // hello pane-replay lands we explicitly drop the viewport to the
  // bottom so xterm's auto-follow can't get stuck pinned at the TOP
  // of the just-replayed buffer (the "I reconnected and now I'm
  // looking at the OLDEST scrollback" symptom).
  //
  // CRITICAL: the scroll has to happen via term.write()'s completion
  // callback, NOT synchronously after writeToTerm() returns. xterm's
  // write queue is async — running scrollToBottom() too early
  // scrolls to the bottom of the still-empty buffer (which becomes
  // the TOP once the replay processes microseconds later), exactly
  // the bug we're trying to fix.
  let bottomOnNextOutput = false;
  // Working directory of this tmux session as reported by the
  // server's hello. Used to default file/directory-browse dialogs
  // (statusbar Files pill, mobile composer's folder button) to the
  // project root the user is actually working in, rather than the
  // fs jail root. Null until hello arrives (or on resolve failure).
  let sessionCwd: string | null = null;
  let mic: MicStreamerType | null = null;
  const applyMicConfig = (cfg: MicConfig): void => {
    mic?.setConfig({
      autoStopEnabled: cfg.auto_stop_enabled,
      silenceMs: cfg.silence_ms,
      maxRecordingSeconds: cfg.max_recording_seconds,
    });
  };
  // Pass the session name so mute state is scoped per-session — muting
  // one conversation no longer silences a sibling tab.
  const tts: TtsPlayerType = new TtsPlayer(session);
  // Mirror the mute toggle to the server so it can skip the Kokoro
  // round-trip while we're not listening. Fire-and-forget; the server
  // re-reads on the next message if the send is silently dropped.
  tts.onMutedChange = (muted) => {
    socket.send({ type: "tts_mute", value: muted });
  };
  // Caches the most recent assistant text so the "replay" pill can ask
  // the backend to re-synthesize it. Updated on each tts_start.
  let lastSpokenText = "";

  // ─── Mic-availability pub-sub ────────────────────────────────────────
  // micAvailable is `true` once the server's hello says voice is wired up
  // AND the browser is in a secure context (getUserMedia requires that).
  // The mobile composer mic and the desktop FAB both subscribe so they
  // appear/disappear consistently when the hello eventually arrives —
  // without this, the mobile mic was set hidden at mount time before any
  // hello had landed and never updated.
  let micAvailable = false;
  const availSubs: Array<(a: boolean) => void> = [];
  const setMicAvailable = (a: boolean): void => {
    if (micAvailable === a) return;
    micAvailable = a;
    for (const cb of availSubs) {
      try { cb(a); } catch (e) { console.warn("avail sub failed:", e); }
    }
  };
  const onMicAvailabilityChange = (cb: (a: boolean) => void): (() => void) => {
    availSubs.push(cb);
    cb(micAvailable);
    return () => {
      const idx = availSubs.indexOf(cb);
      if (idx >= 0) availSubs.splice(idx, 1);
    };
  };

  // TTS playback visualiser: a small scope in the statusbar that runs
  // only while audio is actually leaving the speakers. Honest "yes you
  // can hear me" feedback so the user can tell mute / system-vol issues
  // apart from "Claude hasn't replied yet".
  let ttsWaveform: import("./waveform").Waveform | null = null;
  // Wake-lock during TTS playback is now owned by TtsPlayer itself
  // (see tts.ts), which acquires before play() and releases on every
  // exit path including play() rejection — without that, a failed
  // play() acquired the lock but never released it, leaking refcount
  // for the tab's lifetime.
  tts.onPlaybackStart = () => {
    const an = tts.getAnalyser();
    if (an) {
      ttsWaveCanvas.hidden = false;
      if (!ttsWaveform) ttsWaveform = new Waveform(ttsWaveCanvas, an);
      ttsWaveform.start();
    }
  };
  tts.onPlaybackEnd = () => {
    ttsWaveform?.stop();
    ttsWaveCanvas.hidden = true;
  };

  const updateTtsBtn = () => {
    const muted = tts.isMuted;
    ttsBtn.setAttribute("aria-pressed", muted ? "false" : "true");
    ttsBtn.innerHTML = `${muted ? TTS_MUTED_SVG : TTS_SVG} <span>${muted ? "muted" : "voice"}</span>`;
  };
  updateTtsBtn();

  // ─── Recording state (single source of truth, both UIs subscribe) ─────
  const VOICE_TRIGGER = "\x1bk";          // matches the meta+k keybinding
  // Wait between sending meta+k (which arms claude's /voice) and
  // opening the browser mic. The pipeline is: meta+k → tmux → claude
  // parses it → claude enters /voice mode → claude reads PCM from
  // Pulse. If the mic opens before that whole chain has primed,
  // leading audio frames can be dropped on the claude side. 60 ms
  // was the original value and turned out to truncate the start of
  // utterances; 200 ms gives the chain comfortable headroom while
  // still being imperceptible from the user's tap-to-speak rhythm.
  const TRIGGER_TO_AUDIO_DELAY_MS = 200;
  // The release-PTT timing on stop used to live here as a fixed
  // post-stop delay (AUDIO_TO_TRIGGER_DELAY_MS). The backend now
  // owns it: on `mic_stop` it estimates pipeline drain from
  // bytes-written stats and adds the configured drain_pad_ms, then
  // writes the release keystroke itself. So the client just sends
  // `mic_stop` and walks away.

  let recording = false;
  const stateSubs: Array<(r: boolean) => void> = [];
  const setRecording = (r: boolean): void => {
    if (recording === r) return;
    recording = r;
    for (const cb of stateSubs) {
      try { cb(r); } catch (e) { console.warn("rec sub failed:", e); }
    }
  };

  // Tracks whether the in-flight recording was started via PTT hold
  // (so we know whether to auto-stop on hold-end vs leave it running
  // because it was tap-toggled on).
  let pttHoldActive = false;
  // Serializer: queue mic-events so a fast press/release can't race an
  // in-flight toggleMic(). Without this, hold-end runs while
  // toggleMic's await mic.start() is still pending — recording is
  // still false, hold-end returns no-op, then toggleMic resolves with
  // the stream running and no event left to stop it.
  let micEventQueue: Promise<void> = Promise.resolve();

  const handleMicEvent = (kind: "tap" | "hold-start" | "hold-end"): void => {
    micEventQueue = micEventQueue.then(async () => {
      if (!micAvailable) return;
      if (kind === "tap") {
        pttHoldActive = false;
        await toggleMic();
        return;
      }
      if (kind === "hold-start") {
        pttHoldActive = true;
        if (!recording) await toggleMic();
        return;
      }
      if (kind === "hold-end") {
        if (!pttHoldActive) return;        // wasn't a PTT-initiated recording
        pttHoldActive = false;
        if (recording) await toggleMic();
      }
    }).catch((e) => { console.warn("mic event failed:", e); });
  };

  // Option+Space global push-to-talk. Captured on document so it
  // works whether the terminal, the controls strip, or nothing has
  // focus. See ptt.ts for the chord rules and the blur-safety net.
  // Unbind on session dispose — without this, switching sessions
  // stacks a new pair of listeners on each attach and every
  // Option+Space would fire N onHoldStarts.
  const detachPtt = attachOptionSpacePtt({
    onHoldStart: () => handleMicEvent("hold-start"),
    onHoldEnd: () => handleMicEvent("hold-end"),
  });
  view.addEventListener("ccpipe:dispose", detachPtt, { once: true });

  const toggleMic = async (): Promise<void> => {
    if (!micAvailable) return;
    if (recording) {
      // Stop: tear down browser mic, then hand the release-PTT timing
      // off to the backend via mic_stop. The backend knows how many
      // bytes are still draining through Pulse and how much pad to
      // add for claude's STT finalisation — far more accurate than
      // any fixed client-side delay.
      setRecording(false);
      try { await mic?.stop(); } catch {}
      socket.send({ type: "mic_stop" });
      void wakeLock.release();
      // Refocus xterm so a physical Enter works immediately to send
      // the transcribed utterance. Without this, the mic button
      // retains focus and the user's first Enter is a no-op until
      // they click back into the terminal.
      terminalApi?.term.focus();
    } else {
      setRecording(true);
      socket.send({ type: "input", data: VOICE_TRIGGER });
      void wakeLock.acquire();
      await new Promise((r) => setTimeout(r, TRIGGER_TO_AUDIO_DELAY_MS));
      if (!recording) { void wakeLock.release(); return; }
      try {
        await mic?.start();
        // Auto-stop when the user goes silent for ~1.5s. This makes the
        // mic behave like a sensible voice assistant rather than
        // streaming forever until manually stopped. Route through
        // handleMicEvent("tap") so the PTT state machine (pttHoldActive)
        // is reset in the same place a manual stop would reset it.
        if (mic) mic.onSilence = () => { handleMicEvent("tap"); };
      } catch (err) {
        console.warn("mic start failed:", err);
        setRecording(false);
        socket.send({ type: "input", data: VOICE_TRIGGER });  // unwind /voice
        void wakeLock.release();
      }
    }
  };

  const socket = new TerminalSocket(wsUrlFor(session), {
    onStatus(status, info) {
      dot.className = "statusbar__dot " + (
        status === "open" ? "ok"
        : status === "closed" ? "err"
        : "warn"
      );
      if (status === "reconnecting") {
        stateLabel.textContent = info?.nextRetryMs
          ? `retrying in ${Math.ceil(info.nextRetryMs / 1000)}s (${info.attempt})`
          : `retrying (${info?.attempt ?? "?"})`;
        lastRetryInfo = info ?? null;
        retryDeadlineMs = info?.nextRetryMs ? Date.now() + info.nextRetryMs : 0;
        // After 10s of failed retries, escalate to a banner with a
        // manual retry button. If the banner is already showing, refresh
        // its countdown text immediately.
        if (offlineBanner) {
          renderOfflineBanner();
        } else if (offlineTimer === null) {
          offlineTimer = window.setTimeout(() => {
            offlineTimer = null;
            renderOfflineBanner();
            // Tick the countdown each second so the user sees progress.
            bannerCountdownTimer = window.setInterval(renderOfflineBanner, 1000);
          }, 10_000);
        }
      } else if (status === "connecting") {
        stateLabel.textContent = "connecting";
      } else if (status === "open") {
        stateLabel.textContent = "open";
        hideOfflineBanner();
      } else {
        stateLabel.textContent = "closed";
        hideOfflineBanner();
      }
      // Latency reading goes stale once the WS isn't open; hide it
      // rather than letting "423 ms" linger over a closed connection.
      if (status !== "open") latencyLabel.hidden = true;
      // Any non-open status means mic frames are no longer being
      // delivered. Drop recording state + tear the device down so a
      // dropped WS in the middle of dictation doesn't leave the UI
      // believing it's still capturing (and silently dropping frames
      // into a closed sendBinary).
      if (status !== "open" && recording) {
        setRecording(false);
        // Reset the PTT state machine and detach the silence callback
        // here too: otherwise a VAD/max-rec silence callback that was
        // already armed can fire AFTER the drop, re-enter
        // handleMicEvent("tap") → toggleMic with recording=false, and
        // start a brand-new /voice recording over the (now dead) socket.
        pttHoldActive = false;
        if (mic) mic.onSilence = null;
        try { mic?.stop(); } catch {}
      }
      setConnected(status === "open");
      // On every fresh connection, push the current mute state so the
      // server starts in agreement. setMuted's onMutedChange only fires
      // on transitions, so a session that was already muted before
      // attach would otherwise leave the server thinking we're
      // listening and synthesising for nothing.
      if (status === "open") {
        socket.send({ type: "tts_mute", value: tts.isMuted });
      }
    },
    onHello(msg) {
      // Seamless reconnect (short-gap reconnect to the same session): the
      // existing buffer is still accurate, and ws.ts drops the pane replay,
      // so we must NOT wipe and must NOT yank the view to the bottom — the
      // live stream (incl. tmux's attach redraw) just resumes in place and
      // the user's scroll position is preserved. No flicker, no jump.
      if (!socket.seamlessReconnect) {
        // Reset xterm BEFORE the server's pane-replay bytes arrive on the
        // wire so the replay replaces, not appends. On initial connect this
        // is a no-op (buffer is empty); on a long-gap reconnect it discards
        // stale content so the replay accurately reflects tmux's current
        // pane — closing the "new output not in scrollback" gap.
        resetTerminal?.();
        // Arm a one-shot "scroll to bottom" for the first PTY chunk that
        // arrives next (the history replay). After reset() xterm.js can
        // leave its auto-follow flag such that it won't track the cursor as
        // the replay scrolls into scrollback — the viewport stays at
        // scrollTop=0 showing the OLDEST entries. Forcing scrollToBottom
        // after the first post-hello write restores auto-follow for the
        // live tail that follows.
        bottomOnNextOutput = true;
      }
      renderSessionLabel(sessionLabel, msg.session);
      // Cache the session's working dir so the Files pill + the
      // mobile composer's folder button default to the project root
      // instead of $HOME. Falls through to the fs config root if the
      // server couldn't resolve it.
      sessionCwd = msg.cwd ?? null;
      const secure = isSecureContext();
      setMicAvailable(!!(msg.voice && secure));
      if (msg.voice && !secure) showHttpsBanner();

      if (micAvailable && !mic) {
        mic = new MicStreamer(socket);
        // Fetch the persisted voice-input settings and apply once
        // they land. Mic stays at safe defaults until the round-trip
        // finishes; a slow GET doesn't block the UI from working.
        void getMicConfig()
          .then((cfg) => applyMicConfig(cfg))
          .catch((e) => { console.warn("mic config fetch failed:", e); });
      }
      if (!mobile) {
        // Desktop FAB updates here. Mobile composer subscribes to
        // setMicAvailable via the adapter and updates itself.
        micFab.hidden = !micAvailable;
      }
      ttsBtn.hidden = !msg.tts;
      updateTtsBtn();
    },
    onSessionEvent(msg) {
      const noisy = new Set([
        "client-attached", "client-detached",
        "window-renamed", "session-renamed",
      ]);
      if (noisy.has(msg.event)) flashState(stateLabel, msg.event);
    },
    onSessionGone(msg) {
      flashState(stateLabel, `${msg.session} closed`, 4000);
      socket.close();
      setTimeout(bootstrap, 1200);
    },
    onAuthRevoked(reason) {
      // Server closed the WS with 1008 — auth lost, origin rejected,
      // or credentials rotated mid-session (M2 kick). The socket has
      // already self-closed and stopped retrying; bootstrap re-fetches
      // /api/auth/status and routes the user back to login if needed.
      flashState(stateLabel, reason || "signed out", 4000);
      setTimeout(bootstrap, 800);
    },
    onTtsStart(msg) {
      tts.onStart();
      lastSpokenText = msg.text || "";
      updateReplayBtn();
      // Claude is now speaking back, which means /voice has already
      // finished and transcribed. If the UI still thinks it's recording
      // (auto-stop on silence, etc.), reconcile.
      if (recording) {
        setRecording(false);
        try { mic?.stop(); } catch {}
      }
    },
    onTtsAudio(chunk) { tts.onChunk(chunk); },
    onTtsEnd() {
      tts.onEnd();
      // Notify if the tab is backgrounded (user opted in via settings).
      notifications.fireResponseReady(lastSpokenText, session);
    },
    onOutput(data) {
      if (bottomOnNextOutput) {
        bottomOnNextOutput = false;
        // Scroll via the write() completion callback so we definitely
        // run AFTER xterm has processed the bytes and grown its buffer.
        writeToTerm?.(data, () => scrollTerminalToBottom?.());
      } else {
        writeToTerm?.(data);
      }
    },
    onLatency(ms) {
      // Bucket the number so the UI reads as "fine / busy / janky"
      // without me having to read the digits. Hide the label when no
      // measurement yet so it doesn't flash garbage at connect time.
      latencyLabel.hidden = false;
      latencyLabel.textContent = `${ms} ms`;
      latencyLabel.classList.toggle("statusbar__latency--ok", ms < 100);
      latencyLabel.classList.toggle("statusbar__latency--warn", ms >= 100 && ms < 300);
      latencyLabel.classList.toggle("statusbar__latency--bad", ms >= 300);
    },
  });

  const terminalApi = createTerminal(terminalContainer, socket, loadDisplayPrefs(session), session);
  writeToTerm = terminalApi.writeToTerm;
  resetTerminal = terminalApi.resetBuffer;
  scrollTerminalToBottom = terminalApi.scrollToBottom;

  // Ctrl+Shift+D — capture a frontend diagnostic snapshot (WS counters
  // + xterm buffer state + scrollback tail) and pop the debug modal.
  // The same affordance is mirrored in Settings → Debug for keyboard-
  // less users; both paths go through the same captureSnapshot()
  // helper so the report shape stays consistent. The disposed flag
  // closes a race where a previous attachTerminal()'s listener
  // survives long enough to fire AFTER its terminalApi has been
  // disposed — touching term.cols / term.buffer.active on a disposed
  // terminal throws. The ccpipe:dispose handler below sets it before
  // removing the listener.
  let debugListenerDisposed = false;
  const onDebugKey = (e: KeyboardEvent) => {
    if (debugListenerDisposed) return;
    if (!e.ctrlKey || !e.shiftKey) return;
    if (e.key !== "D" && e.key !== "d") return;
    e.preventDefault();
    e.stopPropagation();
    void import("./debug").then(({ captureSnapshot, showDebugModal }) => {
      if (debugListenerDisposed) return;
      const snap = captureSnapshot({ session, terminal: terminalApi, socket });
      showDebugModal(snap);
    });
  };
  document.addEventListener("keydown", onDebugKey);

  // Cross-tab pref sync: if another tab changes display settings, mirror them here.
  const unsubPrefs = onDisplayPrefsChange((p) => terminalApi.applyPrefs(p));
  view.addEventListener("ccpipe:dispose", () => {
    unsubPrefs();
    debugListenerDisposed = true;
    document.removeEventListener("keydown", onDebugKey);
    // Close the socket FIRST so its lifecycle hooks (online /
    // visibilitychange) stop firing and a zombie reconnect can't add a
    // second subscription on the backend — which the user hears as
    // duplicated TTS audio. Every other dispose path (brand click,
    // session_gone, settings invalidate) already closes the socket;
    // doing it here too means a forgotten-cleanup bug in a new path
    // can't reintroduce the duplication.
    try { socket.close(); } catch {}
    try { tts.dispose(); } catch (e) { console.warn("tts dispose failed:", e); }
    try { mic?.stop(); } catch {}
    try { terminalApi.dispose(); } catch (e) { console.warn("terminal dispose failed:", e); }
  }, { once: true });

  // ─── Wire the mic controller into both UIs ────────────────────────────
  if (mobile) {
    mountMobileUI(view, socket, {
      get available() { return micAvailable; },
      onMicEvent: (kind) => handleMicEvent(kind),
      onStateChange: (cb) => {
        stateSubs.push(cb);
        return () => {
          const idx = stateSubs.indexOf(cb);
          if (idx >= 0) stateSubs.splice(idx, 1);
        };
      },
      attachWaveform: (canvas) => {
        const analyser = mic?.getAnalyser();
        if (!analyser) return null;
        return new Waveform(canvas, analyser);
      },
      onConnectionChange,
      onAvailabilityChange: onMicAvailabilityChange,
      getSessionCwd: () => sessionCwd,
    });
  } else {
    // Desktop FAB: same tap-to-toggle behaviour
    micFab.addEventListener("click", () => { void toggleMic(); });
    stateSubs.push((r) => micFab.classList.toggle("recording", r));
  }

  // TTS toggle
  ttsBtn.addEventListener("click", () => {
    tts.setMuted(!tts.isMuted);
    updateTtsBtn();
  });

  socket.connect();
}

// ─── Helpers ────────────────────────────────────────────────────────────

function flashState(el: HTMLElement, msg: string, ms: number = 2200): void {
  const prev = el.textContent ?? "";
  el.textContent = msg;
  el.dataset.flashing = "true";
  setTimeout(() => {
    if (el.dataset.flashing === "true" && el.textContent === msg) {
      el.textContent = prev;
    }
    delete el.dataset.flashing;
  }, ms);
}

/** Rewrite the statusbar session label as ``<span class="key">@</span>
 * NAME`` using DOM nodes. The session name itself goes through
 * textContent so there's no markup interpolation surface — a name
 * containing ``<`` or ``"`` can't escape into HTML even if the
 * server's safe_name validation regresses. Replaces the prior
 * ``escapeHtml + innerHTML`` pattern. */
function renderSessionLabel(el: HTMLElement, session: string): void {
  el.textContent = "";
  const key = document.createElement("span");
  key.className = "key";
  key.textContent = "@";
  el.append(key, " " + session);
}

// ─── Boot ───────────────────────────────────────────────────────────────

// Monotonic bootstrap generation. bootstrap() can be triggered from
// several independent callbacks (onSessionGone, onAuthRevoked, brand
// click, settings onSessionInvalidated, login onSuccess) and it awaits
// network round-trips before mounting. Without a guard, two overlapping
// runs both clear .terminal-view (which fires no dispose for an
// innerHTML wipe), so the slower run mounts a second live socket while
// the first's socket keeps reconnecting/subscribing in the background —
// the duplicate-subscription / duplicate-TTS class we work hard to
// avoid. Each run captures its generation; if a newer run started while
// it was awaiting, the stale run bails before mounting anything.
let bootstrapGen = 0;

async function bootstrap(): Promise<void> {
  const myGen = ++bootstrapGen;
  const isStale = (): boolean => myGen !== bootstrapGen;
  // Dispatch a tear-down event so any pending key listeners cleanly detach.
  app.querySelector(".terminal-view")?.dispatchEvent(new CustomEvent("ccpipe:dispose"));
  try {
    const status = await fetchAuthStatus();
    if (isStale()) return;
    authRequired = status.required;
    if (status.required && !status.authenticated) {
      // If auth was lost, the stored session is meaningless — clear it
      // so the user doesn't auto-attach right back after re-login.
      clearLastSession();
      renderLogin(app, () => bootstrap());
      return;
    }
  } catch (e) {
    console.warn("auth status failed:", e);
    if (isStale()) return;
    // Do NOT proceed as if unauthenticated — a transient network/5xx
    // failure of the auth probe would otherwise drop the user into the
    // picker (which then 401s on every call) or attach blindly. Show a
    // retry affordance and leave authRequired untouched.
    renderBootstrapError(() => bootstrap());
    return;
  }

  // Auto-attach to the last session if it still exists. A short
  // verification round-trip ensures we don't silently spin up a new
  // session if the user's last one has died.
  const last = loadLastSession();
  if (last) {
    try {
      const res = await fetch("/api/sessions", { credentials: "same-origin" });
      if (isStale()) return;
      if (res.ok) {
        const sessions = await res.json();
        // Guard the shape: a 200 with a non-array body (error envelope,
        // proxy interstitial) would otherwise throw on .some and skip
        // the clearLastSession below, leaving us wedged.
        if (Array.isArray(sessions)
            && sessions.some((s: { name?: string }) => s?.name === last)) {
          void attachTerminal(last);
          return;
        }
      }
      // Last session no longer exists; clear so we don't loop here.
      clearLastSession();
    } catch {
      clearLastSession();
    }
  }

  if (isStale()) return;
  const { renderSessionPicker } = await import("./session-picker");
  if (isStale()) return;
  renderSessionPicker(app, attachTerminal);
}

/** Minimal full-screen retry affordance shown when the initial auth
 * probe fails (network down / backend 5xx). Avoids silently proceeding
 * into a half-broken UI. */
function renderBootstrapError(retry: () => void): void {
  app.replaceChildren();
  const wrap = document.createElement("div");
  wrap.className = "bootstrap-error";
  const msg = document.createElement("p");
  msg.textContent = "Can’t reach the server.";
  const btn = document.createElement("button");
  btn.className = "btn";
  btn.textContent = "Retry";
  btn.addEventListener("click", retry);
  wrap.append(msg, btn);
  app.append(wrap);
}

export function isAuthRequired(): boolean { return authRequired; }

bootstrap();
