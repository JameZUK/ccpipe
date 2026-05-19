// Mobile UI: composer bar (textarea + mic + send), modifier-key row
// above the soft keyboard, realtime waveform overlay during recording.
//
// The mic button lives INSIDE the composer flow (not a fixed-position
// FAB) so the soft keyboard opening doesn't shove it under the user's
// finger and cancel the touch.

import { getFsConfig } from "./api";
import { openDirectoryBrowser } from "./directory-browser";
import { FOLDER_SVG, MIC_SVG, STOP_SVG } from "./icons";
import { commitPendingShare, discardPendingShare, peekPendingShare } from "./main";
import { TerminalSocket } from "./ws";
import type { Waveform } from "./waveform";

export function isMobileLayout(): boolean {
  return window.matchMedia("(pointer: coarse)").matches
    || window.matchMedia("(max-width: 768px)").matches;
}

export type MicEventKind = "tap" | "hold-start" | "hold-end";

export interface MobileMicAdapter {
  /** Whether the mic button should be visible (server reports voice + secure ctx). */
  available: boolean;
  /** Called for every mic-button gesture. The receiver maps:
   *    - "tap"        → toggle (existing tap-to-start / tap-to-stop)
   *    - "hold-start" → push-to-talk pressed, ensure recording is running
   *    - "hold-end"   → push-to-talk released, stop + submit
   */
  onMicEvent(kind: MicEventKind): void;
  /** Subscribes to recording-state changes. Returns an unsubscribe fn. */
  onStateChange(cb: (recording: boolean) => void): () => void;
  /** Lazy-construct a Waveform for the given canvas + the live AnalyserNode. */
  attachWaveform(canvas: HTMLCanvasElement): Waveform | null;
  /** Subscribes to WS connection status changes so the composer can grey
   * out when offline. Returns an unsubscribe fn. The callback should be
   * called once immediately with the current status. */
  onConnectionChange(cb: (connected: boolean) => void): () => void;
  /** Subscribes to mic-availability changes (flipping when the server's
   * hello arrives, or if voice support is toggled later). The callback
   * fires once immediately with the current value. Returns unsubscribe. */
  onAvailabilityChange(cb: (available: boolean) => void): () => void;
  /** Returns the session's current working directory if known, else
   * null. Called lazily so the value reflects the most recent hello
   * even after a WS reconnect. Used to default the composer's folder
   * picker to the project root rather than the fs jail root. */
  getSessionCwd?(): string | null;
}

export interface MobileUI {
  composer: HTMLFormElement;
  modifierRow: HTMLDivElement;
  dispose(): void;
}

export function mountMobileUI(parent: HTMLElement,
                               socket: TerminalSocket,
                               mic: MobileMicAdapter): MobileUI {
  // ─── Composer row ────────────────────────────────────────────────────
  const composer = document.createElement("form");
  composer.className = "composer";

  // Input region: textarea + waveform canvas overlaid in the same slot.
  const inputbox = document.createElement("div");
  inputbox.className = "composer__inputbox";
  const textarea = document.createElement("textarea");
  textarea.className = "composer__input";
  textarea.rows = 1;
  textarea.placeholder = "Type a prompt…";
  textarea.spellcheck = false;
  textarea.autocapitalize = "none";
  textarea.autocomplete = "off";
  const waveCanvas = document.createElement("canvas");
  waveCanvas.className = "composer__wave";
  inputbox.append(textarea, waveCanvas);

  // "Insert a file or directory path" — opens the dir browser focused
  // on the user's home, navigates to a dir or types a path, and
  // splices the chosen path into the composer. Useful for handing
  // claude a project root or a specific filename without typing it
  // byte-by-byte on a phone. Icon is a folder, not the historical
  // paperclip — paperclip implies "attach file contents" which is
  // exactly what this doesn't do.
  const attachBtn = document.createElement("button");
  attachBtn.type = "button";
  attachBtn.className = "composer__attach";
  attachBtn.title = "Insert a file or folder path";
  attachBtn.innerHTML = FOLDER_SVG;
  attachBtn.addEventListener("click", async (e) => {
    e.preventDefault();
    // Open the browser at the session's working directory (the
    // project root the user is actively in) when available — that's
    // almost always what they want. Falls back to the fs jail root
    // if the cwd isn't yet known (hello hasn't arrived) or the
    // server couldn't resolve it.
    let initialPath = mic.getSessionCwd?.() ?? null;
    if (!initialPath) {
      try { initialPath = (await getFsConfig()).root; } catch { initialPath = "/"; }
    }
    openDirectoryBrowser(document.body, {
      initialPath,
      onPick: (p) => {
        const v = textarea.value;
        // Ensure single-space separation around the inserted path so
        // it doesn't slam into adjacent words.
        const sep = v && !/\s$/.test(v) ? " " : "";
        textarea.value = v + sep + p + " ";
        autoresize();
        textarea.focus({ preventScroll: true });
      },
    });
  });

  const micBtn = document.createElement("button");
  micBtn.type = "button";
  micBtn.className = "composer__mic";
  micBtn.title = "Tap to start dictation, tap again to stop";
  micBtn.innerHTML = MIC_SVG;
  // Visibility is driven by the availability subscription below. Hidden
  // initially so it doesn't flash in then out if the hello reports
  // voice as unavailable; the subscription fires once immediately with
  // the current value, so this is settled before paint in practice.
  micBtn.hidden = true;

  // Enter sits right of the textarea now (moved out of the modifier
  // row). type="submit" so a tap triggers the composer's submit
  // handler — same path as the soft-keyboard / keydown Enter, which
  // means it sends the textarea contents + \r and clears, or with an
  // empty textarea sends a bare \r to nudge past a claude prompt.
  const enterBtn = document.createElement("button");
  enterBtn.type = "submit";
  enterBtn.className = "composer__enter";
  enterBtn.title = "Send (Enter)";
  enterBtn.textContent = "Enter";

  composer.append(attachBtn, inputbox, micBtn, enterBtn);

  const autoresize = () => {
    textarea.style.height = "auto";
    textarea.style.height = Math.min(textarea.scrollHeight, 160) + "px";
  };

  // Slash-command palette. When the composer starts with "/", surface
  // the common Claude Code commands as a tap-to-insert list above the
  // input. Static list — Claude's `/help` output isn't machine-readable
  // and adding a backend probe would be overkill for this. Edit
  // SLASH_COMMANDS below to extend.
  const SLASH_COMMANDS: Array<{ cmd: string; hint: string }> = [
    { cmd: "/help",       hint: "list claude's commands" },
    { cmd: "/clear",      hint: "clear the conversation" },
    { cmd: "/exit",       hint: "exit claude" },
    { cmd: "/resume",     hint: "resume a previous session" },
    { cmd: "/compact",    hint: "compact conversation context" },
    { cmd: "/cost",       hint: "show current session cost" },
    { cmd: "/model",      hint: "switch claude model" },
    { cmd: "/init",       hint: "scaffold a CLAUDE.md for this project" },
    { cmd: "/review",     hint: "review code in this dir" },
    { cmd: "/status",     hint: "show session status" },
    { cmd: "/config",     hint: "edit claude settings" },
    { cmd: "/permissions", hint: "manage tool permissions" },
  ];
  const slashList = document.createElement("div");
  slashList.className = "slash-palette";
  slashList.hidden = true;
  inputbox.appendChild(slashList);

  const updateSlashPalette = () => {
    const v = textarea.value;
    if (!v.startsWith("/")) {
      slashList.hidden = true;
      slashList.innerHTML = "";
      return;
    }
    const q = v.toLowerCase();
    const matches = SLASH_COMMANDS.filter(c => c.cmd.startsWith(q));
    if (matches.length === 0) {
      slashList.hidden = true;
      slashList.innerHTML = "";
      return;
    }
    slashList.hidden = false;
    slashList.innerHTML = "";
    for (const c of matches) {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "slash-palette__row";
      row.innerHTML =
        `<span class="slash-palette__cmd"></span>` +
        `<span class="slash-palette__hint"></span>`;
      row.querySelector<HTMLElement>(".slash-palette__cmd")!.textContent = c.cmd;
      row.querySelector<HTMLElement>(".slash-palette__hint")!.textContent = c.hint;
      row.addEventListener("pointerdown", (e) => {
        // Use pointerdown not click — click is preceded by textarea
        // blur which would hide the palette before we can insert.
        e.preventDefault();
        textarea.value = c.cmd + " ";
        autoresize();
        slashList.hidden = true;
        slashList.innerHTML = "";
        textarea.focus();
      });
      slashList.appendChild(row);
    }
  };

  textarea.addEventListener("input", autoresize);
  textarea.addEventListener("input", updateSlashPalette);
  textarea.addEventListener("focus", updateSlashPalette);
  textarea.addEventListener("blur", () => {
    // Delay so a pointerdown on a row has time to fire before the
    // palette is torn down by blur.
    setTimeout(() => { slashList.hidden = true; }, 120);
  });
  textarea.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      composer.requestSubmit();
    }
  });
  // beforeinput catches the soft-keyboard Enter on Android — Gboard
  // (and most IME-driven keyboards) doesn't fire `keydown` with
  // `key === "Enter"`; it reports `key: "Unidentified"` / `keyCode: 229`
  // during composition and dispatches the line break via `beforeinput`
  // with `inputType: "insertLineBreak"`. Without this hook the keydown
  // handler above misses it, default behaviour inserts \n into the
  // textarea, and the user sees "Enter just makes a new line".
  textarea.addEventListener("beforeinput", (e) => {
    const it = (e as InputEvent).inputType;
    if (it === "insertLineBreak" || it === "insertParagraph") {
      e.preventDefault();
      composer.requestSubmit();
    }
  });

  composer.addEventListener("submit", (e) => {
    e.preventDefault();
    const v = textarea.value;
    if (v) {
      // Append \r, not \n. Claude Code's TUI runs the PTY in raw mode
      // and interprets carriage-return as "submit prompt" — the same
      // byte a physical Enter from xterm produces. With \n the line
      // lands in claude's input field but the prompt never fires.
      socket.send({ type: "input", data: v + "\r" });
      textarea.value = "";
      autoresize();
      return;
    }
    // Empty composer + Enter: nudge claude past a TUI prompt that
    // expects a bare keypress (e.g. "press Enter to continue") without
    // the user having to type a space first. Matches what the old
    // modifier-row Enter did before it was promoted into the composer.
    socket.send({ type: "input", data: "\r" });
  });

  // Mic gestures. Tap = toggle (existing behaviour). Long-press =
  // push-to-talk: release submits. Pointer-capture keeps events flowing
  // even if the user's finger slides off the button mid-hold.
  const HOLD_MS = 250;
  let pressTimer: number | null = null;
  let activePointer: number | null = null;
  let isHolding = false;

  const cancelTimer = () => {
    if (pressTimer !== null) { clearTimeout(pressTimer); pressTimer = null; }
  };
  micBtn.addEventListener("pointerdown", (e) => {
    if (activePointer !== null) return;        // ignore concurrent fingers
    e.preventDefault();
    activePointer = e.pointerId;
    try { micBtn.setPointerCapture(e.pointerId); } catch {}
    pressTimer = window.setTimeout(() => {
      isHolding = true;
      pressTimer = null;
      mic.onMicEvent("hold-start");
    }, HOLD_MS);
  });
  micBtn.addEventListener("pointerup", (e) => {
    if (e.pointerId !== activePointer) return;
    activePointer = null;
    if (isHolding) {
      isHolding = false;
      mic.onMicEvent("hold-end");
    } else {
      cancelTimer();
      mic.onMicEvent("tap");
    }
  });
  micBtn.addEventListener("pointercancel", (e) => {
    if (e.pointerId !== activePointer) return;
    activePointer = null;
    if (isHolding) {
      isHolding = false;
      mic.onMicEvent("hold-end");
    } else {
      // System cancel without a release — don't fire tap, just unwind.
      cancelTimer();
    }
  });
  // Keyboard activation (Tab + Enter / Space) — pointer events skip this.
  micBtn.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      mic.onMicEvent("tap");
    }
  });

  // Composer disable when WS is not open. Don't silently swallow typed
  // input — show the user the state of the world.
  const unsubConn = mic.onConnectionChange((connected) => {
    composer.classList.toggle("offline", !connected);
    textarea.disabled = !connected || composer.classList.contains("recording");
    micBtn.disabled = !connected;
    enterBtn.disabled = !connected || composer.classList.contains("recording");
    textarea.placeholder = connected ? "Type a prompt…" : "offline — waiting to reconnect…";
  });

  // Mic visibility: hide when voice support hasn't arrived (or is gone).
  // The callback fires once immediately with the current value, then
  // again whenever main.ts flips micAvailable in onHello.
  const unsubAvail = mic.onAvailabilityChange((avail) => {
    micBtn.hidden = !avail;
  });

  let waveform: Waveform | null = null;
  // Recording starts async (await mic.start() ahead of the analyser
  // existing). State-change fires immediately on toggle, so we may try
  // to attach the waveform before the AnalyserNode is wired. Retry on a
  // short cadence until either it attaches or the user toggles off.
  let attachAttempts = 0;
  let attachTimer: number | null = null;
  const MAX_ATTACH_ATTEMPTS = 80;        // 80 * 50ms = 4s
  const ATTACH_RETRY_MS = 50;
  const clearAttachTimer = () => {
    if (attachTimer !== null) {
      clearTimeout(attachTimer);
      attachTimer = null;
    }
  };
  const tryAttachWaveform = () => {
    attachTimer = null;
    if (!composer.classList.contains("recording")) return;
    if (waveform) return;
    waveform = mic.attachWaveform(waveCanvas);
    if (waveform) {
      waveform.start();
      return;
    }
    if (attachAttempts++ < MAX_ATTACH_ATTEMPTS) {
      // Track the handle so a state-change to recording=false can
      // cancel the chain — otherwise a quick toggle-off-then-on would
      // leave the old retry chain alive, racing the new one and
      // potentially double-attaching two Waveform instances on the
      // same canvas.
      attachTimer = window.setTimeout(tryAttachWaveform, ATTACH_RETRY_MS);
    }
  };

  const unsubscribe = mic.onStateChange((recording) => {
    composer.classList.toggle("recording", recording);
    micBtn.classList.toggle("recording", recording);
    micBtn.innerHTML = recording ? STOP_SVG : MIC_SVG;
    micBtn.title = recording
      ? "Tap to stop dictation"
      : "Tap to start dictation, tap again to stop";
    textarea.disabled = recording;
    enterBtn.disabled = recording;
    if (recording) {
      clearAttachTimer();
      attachAttempts = 0;
      tryAttachWaveform();
    } else {
      clearAttachTimer();
      waveform?.stop();
      waveform?.dispose();
      waveform = null;
    }
  });

  // ─── Modifier-key row ────────────────────────────────────────────────
  // Always visible — previously it only showed when the soft keyboard
  // was on screen (visualViewport height collapsed). That hid the row
  // for any landscape-keyboard / hardware-keyboard / tablet scenario
  // and made the Esc/Tab/Enter/arrow shortcuts unreachable.
  const modifierRow = document.createElement("div");
  modifierRow.className = "modifier-row";

  let ctrlArmed = false;
  const setCtrl = (on: boolean) => {
    ctrlArmed = on;
    modifierRow.querySelector('[data-key="ctrl"]')?.classList.toggle("armed", on);
  };

  const KEYS: Array<{ label: string; key: string; bytes?: string }> = [
    { label: "Esc", key: "esc", bytes: "\x1b" },
    { label: "Tab", key: "tab", bytes: "\t" },
    // "/" opens claude's slash-command menu — kept here as a quick-tap
    // shortcut so the user doesn't have to switch keyboard layers on
    // mobile. Enter used to live next to it; it now sits on the
    // composer row (composer__enter) for a more obvious "send" target.
    { label: "/", key: "slash", bytes: "/" },
    { label: "Ctrl", key: "ctrl" },
    { label: "↑", key: "up", bytes: "\x1b[A" },
    { label: "↓", key: "down", bytes: "\x1b[B" },
    { label: "←", key: "left", bytes: "\x1b[D" },
    { label: "→", key: "right", bytes: "\x1b[C" },
  ];
  // Hold-to-repeat: a press-and-hold on Esc/arrows/Tab/Enter/"/" should
  // behave like a physical key — one tap fires once, holding it down
  // repeats at xterm-ish cadence (400ms initial delay, then 30ms steps).
  // Ctrl is a modifier toggle and explicitly opt-OUT of repeat. We
  // implement via pointer events rather than `click` so we own the
  // gesture from press to release; that also lets us cancel cleanly
  // if the finger slides off (pointercancel) or the OS reclaims focus.
  const REPEAT_DELAY_MS = 400;
  const REPEAT_INTERVAL_MS = 30;
  for (const k of KEYS) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = k.label;
    btn.dataset.key = k.key;
    if (k.key === "ctrl") {
      // Ctrl arms the next regular key; no repeat semantics.
      btn.addEventListener("click", (e) => {
        e.preventDefault();
        setCtrl(!ctrlArmed);
      });
      modifierRow.append(btn);
      continue;
    }
    let initialTimer: number | null = null;
    let repeatTimer: number | null = null;
    const fire = () => {
      socket.send({ type: "input", data: k.bytes ?? "" });
    };
    const stop = () => {
      if (initialTimer !== null) { clearTimeout(initialTimer); initialTimer = null; }
      if (repeatTimer !== null) { clearInterval(repeatTimer); repeatTimer = null; }
    };
    btn.addEventListener("pointerdown", (e) => {
      // Primary button only. Mouse right-click and touch-secondary
      // shouldn't fire.
      if (e.button !== 0 && e.pointerType !== "touch") return;
      e.preventDefault();
      try { btn.setPointerCapture(e.pointerId); } catch {}
      fire();
      initialTimer = window.setTimeout(() => {
        repeatTimer = window.setInterval(fire, REPEAT_INTERVAL_MS);
      }, REPEAT_DELAY_MS);
    });
    btn.addEventListener("pointerup", () => { stop(); });
    btn.addEventListener("pointercancel", () => { stop(); });
    btn.addEventListener("pointerleave", () => { stop(); });
    // Defence in depth: if the page is hidden mid-press (lock screen,
    // app switch), kill the repeat so we don't stream characters into
    // a backgrounded tab.
    btn.addEventListener("lostpointercapture", () => { stop(); });
    modifierRow.append(btn);
  }
  textarea.addEventListener("keypress", (e) => {
    if (!ctrlArmed) return;
    e.preventDefault();
    const c = e.key.length === 1 ? e.key.toLowerCase() : null;
    if (c && c >= "a" && c <= "z") {
      const code = c.charCodeAt(0) - 96;
      socket.send({ type: "input", data: String.fromCharCode(code) });
    }
    setCtrl(false);
  });

  parent.append(composer, modifierRow);

  // PWA share_target hand-off: if the user shared text into ccpipe
  // from another app, render an explicit review chip ABOVE the composer
  // showing the text and asking [insert] / [discard]. The pre-fix
  // version dropped the shared text straight into the prompt and
  // silently scrubbed the URL, which made a crafted link from any
  // chat/email client a usable command-injection social-engineering
  // vector for a shell-adjacent tool.
  const pending = peekPendingShare();
  if (pending) {
    const review = document.createElement("div");
    review.className = "share-review";
    review.setAttribute("role", "dialog");
    review.setAttribute("aria-label", "Shared text — review before insert");

    const label = document.createElement("div");
    label.className = "share-review__label";
    label.textContent = "Shared text received";

    const body = document.createElement("pre");
    body.className = "share-review__body";
    body.textContent = pending;

    const actions = document.createElement("div");
    actions.className = "share-review__actions";

    const insertBtn = document.createElement("button");
    insertBtn.type = "button";
    insertBtn.className = "btn btn--primary";
    insertBtn.textContent = "insert";
    insertBtn.addEventListener("click", () => {
      const text = commitPendingShare();
      if (text) {
        textarea.value = text;
        autoresize();
        textarea.focus({ preventScroll: true });
      }
      review.remove();
    });

    const discardBtn = document.createElement("button");
    discardBtn.type = "button";
    discardBtn.className = "btn btn--ghost";
    discardBtn.textContent = "discard";
    discardBtn.addEventListener("click", () => {
      discardPendingShare();
      review.remove();
    });

    actions.append(discardBtn, insertBtn);
    review.append(label, body, actions);
    // Insert above the composer so it's the first thing the operator
    // sees on session-open. They have to actively choose to insert.
    composer.parentElement?.insertBefore(review, composer);
  }

  // Focus the composer so the user can start typing immediately on
  // session open — without this they have to tap the textarea first to
  // get the soft keyboard up. We're inside a user-gesture context
  // (session-pick tap) so iOS / Android browsers should honour focus().
  textarea.focus({ preventScroll: true });

  return {
    composer,
    modifierRow,
    dispose() {
      unsubscribe();
      unsubConn();
      unsubAvail();
      waveform?.dispose();
      composer.remove();
      modifierRow.remove();
    },
  };
}
