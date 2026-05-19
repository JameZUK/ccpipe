// Settings modal. Three tabs (Display, Voice, Account — with two-factor
// nested under Account) plus an About footer. Modal overlay; Esc and
// click-outside both dismiss.
//
// Voice + TTS settings are persisted server-side via /api/tts/config so
// they apply across devices. Display preferences are local to this
// browser via localStorage (see display-prefs.ts).
//
// The last-visited tab is remembered in localStorage so re-opening the
// modal lands on the same tab the user was on.
//
// To open the modal, call openSettings({...}) from anywhere with access
// to the helpers it needs.

import { getMicConfig, type MicConfig, setMicConfig } from "./api";
import { changeCredentials, logout as apiLogout } from "./auth";
import { TERMINAL_FONTS } from "./terminal-fonts";
import {
  DEFAULT_PREFS,
  DisplayPrefs,
  loadDisplayPrefs,
  saveDisplayPrefs,
} from "./display-prefs";

const ICONS = {
  close: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>`,
  test: `<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><polygon points="8 5 19 12 8 19 8 5"/></svg>`,
  logout: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>`,
};

const VERSION = "0.1.0";

type TabId = "display" | "voice" | "account";
const LS_LAST_TAB = "ccpipe.settings.tab";
const DEFAULT_TAB: TabId = "display";

function loadLastTab(): TabId {
  try {
    const v = localStorage.getItem(LS_LAST_TAB);
    if (v === "display" || v === "voice" || v === "account") return v;
  } catch {}
  return DEFAULT_TAB;
}

function saveLastTab(t: TabId): void {
  try { localStorage.setItem(LS_LAST_TAB, t); } catch {}
}

type TtsScope = "full" | "last_paragraph" | "last_sentence" | "last_question" | "off";
type TtsServerConfig = {
  voice: string;
  speech_rate: number;
  enabled: boolean;
  scope: TtsScope;
};

const SCOPE_OPTIONS: Array<{ value: TtsScope; label: string }> = [
  { value: "full",           label: "Full response" },
  { value: "last_paragraph", label: "Last paragraph" },
  { value: "last_sentence",  label: "Last sentence" },
  { value: "last_question",  label: "Last question (or paragraph)" },
  { value: "off",            label: "Off (don't speak)" },
];

export interface SettingsOpts {
  authRequired: boolean;
  /** Called whenever display prefs change. Live updates from the terminal. */
  onDisplayPrefsChange: (prefs: DisplayPrefs) => void;
  /** Called after logout / credential change so caller can re-bootstrap. */
  onSessionInvalidated: () => void;
  /** Called after the user saves voice-input settings so the live mic
   * streamer can adopt the new VAD / max-record values without waiting
   * for the next page load. */
  onMicConfigChange?: (cfg: MicConfig) => void;
}

let activeOverlay: HTMLDivElement | null = null;

export function openSettings(opts: SettingsOpts): void {
  if (activeOverlay) return;

  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  activeOverlay = overlay;

  const modal = document.createElement("div");
  modal.className = "modal";
  modal.setAttribute("role", "dialog");
  modal.setAttribute("aria-modal", "true");
  modal.setAttribute("aria-label", "Settings");

  // Tab structure: Display (local browser), Voice (server-side TTS),
  // Account (credentials + two-factor). Each tab panel hosts one or two
  // <section> blocks. Two-factor lives inside the Account panel so the
  // identity-related controls are co-located.
  const displayPanel = document.createElement("div");
  displayPanel.className = "modal__panel";
  displayPanel.dataset.tab = "display";
  displayPanel.append(buildDisplaySection(opts));

  const voicePanel = document.createElement("div");
  voicePanel.className = "modal__panel";
  voicePanel.dataset.tab = "voice";
  voicePanel.append(buildVoiceSection(), buildVoiceInputSection(opts));

  const accountPanel = document.createElement("div");
  accountPanel.className = "modal__panel";
  accountPanel.dataset.tab = "account";
  accountPanel.append(buildAccountSection(opts), buildTwoFactorSection());

  const panels: Record<TabId, HTMLElement> = {
    display: displayPanel,
    voice: voicePanel,
    account: accountPanel,
  };

  const initial = loadLastTab();
  saveLastTab(initial);
  const tabs = buildTabs(initial, (next) => {
    for (const id of Object.keys(panels) as TabId[]) {
      panels[id].classList.toggle("modal__panel--active", id === next);
    }
    saveLastTab(next);
  });
  panels[initial].classList.add("modal__panel--active");

  modal.append(buildHeader(), tabs, displayPanel, voicePanel, accountPanel, buildAboutFooter());

  overlay.appendChild(modal);
  document.body.appendChild(overlay);

  // Click outside the modal → close.
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) closeSettings();
  });
  // Esc → close.
  const onKey = (e: KeyboardEvent) => {
    if (e.key === "Escape") {
      e.preventDefault();
      closeSettings();
    }
  };
  document.addEventListener("keydown", onKey);
  overlay.addEventListener("close-cleanup" as any, () => {
    document.removeEventListener("keydown", onKey);
  });

  // First focusable element gets focus for keyboard users.
  setTimeout(() => modal.querySelector<HTMLElement>("input, select, button")?.focus(), 50);
}

export function closeSettings(): void {
  if (!activeOverlay) return;
  activeOverlay.dispatchEvent(new CustomEvent("close-cleanup"));
  activeOverlay.remove();
  activeOverlay = null;
}

// ─── Header ─────────────────────────────────────────────────────────────

function buildHeader(): HTMLElement {
  const head = document.createElement("div");
  head.className = "modal__header";
  head.innerHTML = `
    <div class="modal__title">Settings</div>
    <button class="modal__close" aria-label="Close" type="button">${ICONS.close}</button>
  `;
  head.querySelector<HTMLButtonElement>(".modal__close")!.onclick = closeSettings;
  return head;
}

// ─── Tab bar ────────────────────────────────────────────────────────────

function buildTabs(initial: TabId, onChange: (next: TabId) => void): HTMLElement {
  const bar = document.createElement("div");
  bar.className = "modal__tabs";
  bar.setAttribute("role", "tablist");
  const items: Array<{ id: TabId; label: string }> = [
    { id: "display", label: "display" },
    { id: "voice",   label: "voice"   },
    { id: "account", label: "account" },
  ];
  const buttons: HTMLButtonElement[] = [];
  for (const it of items) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "modal__tab";
    btn.textContent = it.label;
    btn.dataset.tab = it.id;
    btn.setAttribute("role", "tab");
    btn.setAttribute("aria-selected", it.id === initial ? "true" : "false");
    btn.tabIndex = it.id === initial ? 0 : -1;
    if (it.id === initial) btn.classList.add("modal__tab--active");
    btn.addEventListener("click", () => activate(it.id));
    bar.append(btn);
    buttons.push(btn);
  }
  const activate = (next: TabId) => {
    for (const b of buttons) {
      const on = b.dataset.tab === next;
      b.classList.toggle("modal__tab--active", on);
      b.setAttribute("aria-selected", on ? "true" : "false");
      b.tabIndex = on ? 0 : -1;
    }
    onChange(next);
  };
  // Arrow-key navigation between tabs for keyboard users.
  bar.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    const cur = buttons.findIndex((b) => b.getAttribute("aria-selected") === "true");
    const delta = e.key === "ArrowRight" ? 1 : -1;
    const next = (cur + delta + buttons.length) % buttons.length;
    const nextId = buttons[next].dataset.tab as TabId;
    activate(nextId);
    buttons[next].focus();
    e.preventDefault();
  });
  return bar;
}

// ─── Voice section ──────────────────────────────────────────────────────

function buildVoiceSection(): HTMLElement {
  const sec = document.createElement("section");
  sec.className = "modal__section";
  sec.innerHTML = `
    <h2 class="modal__section-title">voice</h2>
    <div class="modal__rows">
      <label class="row">
        <span class="row__label">Voice</span>
        <div class="row__control row__control--inline">
          <select name="voice" class="select" disabled><option>loading…</option></select>
          <button type="button" class="btn btn--ghost btn--icon" data-role="test" title="Play sample">${ICONS.test}<span>test</span></button>
        </div>
      </label>
      <label class="row">
        <span class="row__label">Speech rate <span class="row__hint" data-role="rate-value">1.0×</span></span>
        <input type="range" name="speech_rate" min="0.5" max="2.0" step="0.05" value="1.0" class="slider"/>
      </label>
      <label class="row">
        <span class="row__label">What to read aloud</span>
        <select name="scope" class="select">
          ${SCOPE_OPTIONS.map(o => `<option value="${o.value}">${o.label}</option>`).join("")}
        </select>
      </label>
      <label class="row">
        <span class="row__label">Notify when backgrounded
          <span class="row__hint">browser notification on response, requires permission</span>
        </span>
        <input type="checkbox" name="notify" class="checkbox"/>
      </label>
    </div>
    <div class="modal__row-actions">
      <span class="modal__status" data-role="voice-status"></span>
      <button type="button" class="btn btn--primary" data-role="save">Save voice</button>
    </div>
  `;

  const select = sec.querySelector<HTMLSelectElement>("select[name=voice]")!;
  const rate = sec.querySelector<HTMLInputElement>("input[name=speech_rate]")!;
  const rateValue = sec.querySelector<HTMLElement>("[data-role=rate-value]")!;
  const scopeSelect = sec.querySelector<HTMLSelectElement>("select[name=scope]")!;
  const notifyCheckbox = sec.querySelector<HTMLInputElement>("input[name=notify]")!;
  const testBtn = sec.querySelector<HTMLButtonElement>("[data-role=test]")!;
  const saveBtn = sec.querySelector<HTMLButtonElement>("[data-role=save]")!;
  const status = sec.querySelector<HTMLElement>("[data-role=voice-status]")!;

  // Notification toggle: pure client-side preference, so wire it
  // independently of the Save voice button. Requesting permission must
  // happen from a user gesture (the click here), so do it inline.
  import("./notifications").then(({ isEnabled, setEnabled, requestPermission, notificationsSupported }) => {
    if (!notificationsSupported()) {
      notifyCheckbox.disabled = true;
      notifyCheckbox.title = "this browser doesn't support notifications";
      return;
    }
    notifyCheckbox.checked = isEnabled();
    notifyCheckbox.addEventListener("change", async () => {
      if (notifyCheckbox.checked) {
        const perm = await requestPermission();
        if (perm !== "granted") {
          notifyCheckbox.checked = false;
          status.textContent = "notification permission denied";
          status.classList.add("modal__status--error");
          return;
        }
        setEnabled(true);
      } else {
        setEnabled(false);
      }
    });
  });

  rate.addEventListener("input", () => {
    rateValue.textContent = `${parseFloat(rate.value).toFixed(2)}×`;
  });

  const loadConfig = async () => {
    try {
      const [voicesRes, configRes] = await Promise.all([
        fetch("/api/tts/voices", { credentials: "same-origin" }),
        fetch("/api/tts/config", { credentials: "same-origin" }),
      ]);
      const { voices = [] } = (await voicesRes.json()) as { voices: string[] };
      const cfg = (await configRes.json()) as TtsServerConfig;
      select.innerHTML = "";
      if (voices.length === 0) {
        select.innerHTML = `<option value="">(no voices — is Kokoro reachable?)</option>`;
      } else {
        for (const v of voices) {
          const opt = document.createElement("option");
          opt.value = v;
          opt.textContent = v;
          select.appendChild(opt);
        }
        // If the configured voice isn't in the list, append it so it's preserved.
        if (cfg.voice && !voices.includes(cfg.voice)) {
          const opt = document.createElement("option");
          opt.value = cfg.voice;
          opt.textContent = `${cfg.voice} (current)`;
          select.appendChild(opt);
        }
        select.value = cfg.voice;
      }
      select.disabled = voices.length === 0;
      rate.value = String(cfg.speech_rate);
      rateValue.textContent = `${cfg.speech_rate.toFixed(2)}×`;
      // Default to last_paragraph if the server returned an unknown value.
      const knownScope = SCOPE_OPTIONS.some(o => o.value === cfg.scope);
      scopeSelect.value = knownScope ? cfg.scope : "last_paragraph";
    } catch (err) {
      status.textContent = `failed to load: ${(err as Error).message}`;
      status.classList.add("modal__status--error");
    }
  };
  loadConfig();

  // Test button: plays a sample with the currently-selected voice
  // through the browser audio element (no Web Audio routing — simple
  // playback so it can't break if AudioContext quirks bite).
  let preview: HTMLAudioElement | null = null;
  testBtn.addEventListener("click", () => {
    const voice = select.value;
    if (!voice) return;
    if (preview) { preview.pause(); preview = null; }
    const text = encodeURIComponent("Voice test, one two three.");
    const url = `/api/tts/preview?voice=${encodeURIComponent(voice)}&text=${text}`;
    preview = new Audio(url);
    preview.play().catch((err) => {
      status.textContent = `preview failed: ${err.message ?? err}`;
      status.classList.add("modal__status--error");
    });
  });

  saveBtn.addEventListener("click", async () => {
    status.classList.remove("modal__status--error");
    status.textContent = "saving…";
    saveBtn.disabled = true;
    try {
      const res = await fetch("/api/tts/config", {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-Requested-By": "ccpipe",
        },
        body: JSON.stringify({
          voice: select.value || null,
          speech_rate: parseFloat(rate.value),
          scope: scopeSelect.value as TtsScope,
        }),
      });
      if (!res.ok) throw new Error(`status ${res.status}`);
      status.textContent = "saved";
      setTimeout(() => { status.textContent = ""; }, 1500);
    } catch (err) {
      status.textContent = `save failed: ${(err as Error).message}`;
      status.classList.add("modal__status--error");
    } finally {
      saveBtn.disabled = false;
    }
  });

  return sec;
}

// ─── Voice input (mic) section ──────────────────────────────────────────

function buildVoiceInputSection(opts: SettingsOpts): HTMLElement {
  const sec = document.createElement("section");
  sec.className = "modal__section";
  sec.innerHTML = `
    <h2 class="modal__section-title">voice input</h2>
    <div class="modal__rows">
      <label class="row">
        <span class="row__label">Auto-stop on silence
          <span class="row__hint">stop recording automatically when you stop speaking</span>
        </span>
        <input type="checkbox" name="auto_stop" class="checkbox"/>
      </label>
      <label class="row">
        <span class="row__label">Silence before stop
          <span class="row__hint" data-role="silence-value">2.5s</span>
        </span>
        <input type="range" name="silence_ms" min="500" max="6000" step="100" value="2500" class="slider"/>
      </label>
      <label class="row">
        <span class="row__label">Submit pad
          <span class="row__hint" data-role="drain-value">1.5s — extra wait after recording before claude submits</span>
        </span>
        <input type="range" name="drain_pad_ms" min="0" max="5000" step="100" value="1500" class="slider"/>
      </label>
      <label class="row">
        <span class="row__label">Max recording length
          <span class="row__hint" data-role="max-value">60s</span>
        </span>
        <input type="range" name="max_recording_seconds" min="10" max="300" step="5" value="60" class="slider"/>
      </label>
    </div>
    <div class="modal__row-actions">
      <span class="modal__status" data-role="voice-input-status"></span>
      <button type="button" class="btn btn--primary" data-role="save">Save voice input</button>
    </div>
  `;

  const autoStop = sec.querySelector<HTMLInputElement>("input[name=auto_stop]")!;
  const silence = sec.querySelector<HTMLInputElement>("input[name=silence_ms]")!;
  const silenceLabel = sec.querySelector<HTMLElement>("[data-role=silence-value]")!;
  const drain = sec.querySelector<HTMLInputElement>("input[name=drain_pad_ms]")!;
  const drainLabel = sec.querySelector<HTMLElement>("[data-role=drain-value]")!;
  const max = sec.querySelector<HTMLInputElement>("input[name=max_recording_seconds]")!;
  const maxLabel = sec.querySelector<HTMLElement>("[data-role=max-value]")!;
  const saveBtn = sec.querySelector<HTMLButtonElement>("[data-role=save]")!;
  const status = sec.querySelector<HTMLElement>("[data-role=voice-input-status]")!;

  const fmtSeconds = (ms: number) => `${(ms / 1000).toFixed(ms % 1000 === 0 ? 0 : 1)}s`;
  const updateLabels = () => {
    silenceLabel.textContent = fmtSeconds(parseInt(silence.value, 10));
    drainLabel.textContent = `${fmtSeconds(parseInt(drain.value, 10))} — extra wait after recording before claude submits`;
    maxLabel.textContent = `${max.value}s`;
  };
  silence.addEventListener("input", updateLabels);
  drain.addEventListener("input", updateLabels);
  max.addEventListener("input", updateLabels);
  // When auto-stop is off the silence slider has no effect — visually
  // dim it so the relationship is obvious. Still editable so changing
  // the value mid-disabled is fine; nothing's saved until Save.
  const syncSilenceEnabled = () => {
    silence.disabled = !autoStop.checked;
    silence.style.opacity = autoStop.checked ? "" : "0.5";
  };
  autoStop.addEventListener("change", syncSilenceEnabled);

  (async () => {
    try {
      const cfg = await getMicConfig();
      autoStop.checked = cfg.auto_stop_enabled;
      silence.value = String(cfg.silence_ms);
      drain.value = String(cfg.drain_pad_ms);
      max.value = String(cfg.max_recording_seconds);
      updateLabels();
      syncSilenceEnabled();
    } catch (err) {
      status.textContent = `failed to load: ${(err as Error).message}`;
      status.classList.add("modal__status--error");
    }
  })();

  saveBtn.addEventListener("click", async () => {
    status.classList.remove("modal__status--error");
    status.textContent = "saving…";
    saveBtn.disabled = true;
    try {
      const next = await setMicConfig({
        auto_stop_enabled: autoStop.checked,
        silence_ms: parseInt(silence.value, 10),
        drain_pad_ms: parseInt(drain.value, 10),
        max_recording_seconds: parseInt(max.value, 10),
      });
      opts.onMicConfigChange?.(next);
      status.textContent = "saved";
      setTimeout(() => { status.textContent = ""; }, 1500);
    } catch (err) {
      status.textContent = `save failed: ${(err as Error).message}`;
      status.classList.add("modal__status--error");
    } finally {
      saveBtn.disabled = false;
    }
  });

  return sec;
}

// ─── Account section ────────────────────────────────────────────────────

function buildAccountSection(opts: SettingsOpts): HTMLElement {
  const sec = document.createElement("section");
  sec.className = "modal__section";
  sec.innerHTML = `
    <h2 class="modal__section-title">account</h2>
    <div class="modal__rows">
      <label class="row">
        <span class="row__label">Current password</span>
        <input type="password" name="currentPassword" class="input" autocomplete="current-password"/>
      </label>
      <label class="row">
        <span class="row__label">New username (optional)</span>
        <input type="text" name="newUsername" class="input" autocomplete="username" spellcheck="false"/>
      </label>
      <label class="row">
        <span class="row__label">New password (optional)</span>
        <input type="password" name="newPassword" class="input" autocomplete="new-password"/>
      </label>
    </div>
    <div class="modal__row-actions">
      <span class="modal__status" data-role="account-status"></span>
      <button type="button" class="btn btn--ghost" data-role="signout">${ICONS.logout}<span>sign out</span></button>
      <button type="button" class="btn btn--primary" data-role="save">Save credentials</button>
    </div>
  `;

  const status = sec.querySelector<HTMLElement>("[data-role=account-status]")!;
  const currentPw = sec.querySelector<HTMLInputElement>("input[name=currentPassword]")!;
  const newUser = sec.querySelector<HTMLInputElement>("input[name=newUsername]")!;
  const newPw = sec.querySelector<HTMLInputElement>("input[name=newPassword]")!;
  const saveBtn = sec.querySelector<HTMLButtonElement>("[data-role=save]")!;
  const signOutBtn = sec.querySelector<HTMLButtonElement>("[data-role=signout]")!;

  saveBtn.addEventListener("click", async () => {
    status.classList.remove("modal__status--error");
    if (!currentPw.value) {
      status.textContent = "current password required";
      status.classList.add("modal__status--error");
      return;
    }
    if (!newUser.value && !newPw.value) {
      status.textContent = "set a new username or password";
      status.classList.add("modal__status--error");
      return;
    }
    saveBtn.disabled = true;
    status.textContent = "saving…";
    const result = await changeCredentials({
      currentPassword: currentPw.value,
      newUsername: newUser.value || undefined,
      newPassword: newPw.value || undefined,
    });
    saveBtn.disabled = false;
    if ("error" in result) {
      status.textContent = result.error;
      status.classList.add("modal__status--error");
    } else {
      status.textContent = "credentials updated — re-login required";
      setTimeout(() => {
        closeSettings();
        opts.onSessionInvalidated();
      }, 800);
    }
  });

  signOutBtn.addEventListener("click", async () => {
    await apiLogout().catch(() => {});
    closeSettings();
    opts.onSessionInvalidated();
  });

  return sec;
}

// ─── Display section ────────────────────────────────────────────────────

function buildDisplaySection(opts: SettingsOpts): HTMLElement {
  const sec = document.createElement("section");
  sec.className = "modal__section";
  const prefs = loadDisplayPrefs();
  // Build the font-option list once — same options for both
  // selectors, but each remembers its own selection so a user can
  // run System mono on their desktop and JetBrains Mono on their
  // phone (or vice versa). Mobile-friendly fonts get a glyph hint
  // to nudge users on small screens.
  const fontOptionsHTML = (selected: string) => TERMINAL_FONTS.map(f =>
    `<option value="${f.id}"${f.id === selected ? " selected" : ""}>${f.label}${f.mobileFriendly ? " ★" : ""}</option>`
  ).join("");
  sec.innerHTML = `
    <h2 class="modal__section-title">display</h2>
    <div class="modal__rows">
      <label class="row">
        <span class="row__label">Font size <span class="row__hint" data-role="fontSize-value">${prefs.fontSize}px</span></span>
        <input type="range" name="fontSize" min="11" max="22" step="1" value="${prefs.fontSize}" class="slider"/>
      </label>
      <label class="row">
        <span class="row__label">Line height <span class="row__hint" data-role="lineHeight-value">${prefs.lineHeight.toFixed(2)}</span></span>
        <input type="range" name="lineHeight" min="1.0" max="1.6" step="0.05" value="${prefs.lineHeight}" class="slider"/>
      </label>
      <label class="row">
        <span class="row__label">Letter spacing <span class="row__hint" data-role="letterSpacing-value">${prefs.letterSpacing}px</span></span>
        <input type="range" name="letterSpacing" min="0" max="3" step="0.5" value="${prefs.letterSpacing}" class="slider"/>
      </label>
      <label class="row">
        <span class="row__label">Terminal font · desktop
          <span class="row__hint" data-role="terminalFontDesktop-hint"></span>
        </span>
        <select name="terminalFontDesktop" class="select">
          ${fontOptionsHTML(prefs.terminalFontDesktop)}
        </select>
      </label>
      <label class="row">
        <span class="row__label">Terminal font · mobile
          <span class="row__hint" data-role="terminalFontMobile-hint">★ marks fonts tuned for small screens</span>
        </span>
        <select name="terminalFontMobile" class="select">
          ${fontOptionsHTML(prefs.terminalFontMobile)}
        </select>
      </label>
      <label class="row">
        <span class="row__label">Cursor style</span>
        <select name="cursorStyle" class="select">
          <option value="bar"${prefs.cursorStyle === "bar" ? " selected" : ""}>bar</option>
          <option value="block"${prefs.cursorStyle === "block" ? " selected" : ""}>block</option>
          <option value="underline"${prefs.cursorStyle === "underline" ? " selected" : ""}>underline</option>
        </select>
      </label>
      <label class="row">
        <span class="row__label">Cursor blink</span>
        <input type="checkbox" name="cursorBlink" class="checkbox"${prefs.cursorBlink ? " checked" : ""}/>
      </label>
    </div>
    <div class="modal__row-actions">
      <button type="button" class="btn btn--ghost" data-role="reset">Reset defaults</button>
      <span class="modal__status" data-role="display-status">live</span>
    </div>
  `;

  // Mutable working copy. We seeded it once from loadDisplayPrefs() at
  // section build (above); slider/select handlers mutate it in place and
  // then persist. Without this cache, each `input` event re-parses the
  // localStorage JSON, which on a fast drag fires dozens of times a
  // second per slider.
  const working: DisplayPrefs = { ...prefs };

  const apply = (current: DisplayPrefs) => {
    Object.assign(working, current);
    saveDisplayPrefs(current);
    opts.onDisplayPrefsChange(current);
  };

  const wireRange = (name: keyof DisplayPrefs, fmt: (n: number) => string) => {
    const input = sec.querySelector<HTMLInputElement>(`input[name=${name}]`)!;
    const label = sec.querySelector<HTMLElement>(`[data-role=${name}-value]`)!;
    input.addEventListener("input", () => {
      const next = { ...working, [name]: parseFloat(input.value) };
      label.textContent = fmt(next[name] as number);
      apply(next as DisplayPrefs);
    });
  };
  wireRange("fontSize", (n) => `${Math.round(n)}px`);
  wireRange("lineHeight", (n) => n.toFixed(2));
  wireRange("letterSpacing", (n) => `${n}px`);

  sec.querySelector<HTMLSelectElement>("select[name=cursorStyle]")!
    .addEventListener("change", (e) => {
      apply({ ...working, cursorStyle: (e.target as HTMLSelectElement).value as any });
    });
  sec.querySelector<HTMLInputElement>("input[name=cursorBlink]")!
    .addEventListener("change", (e) => {
      apply({ ...working, cursorBlink: (e.target as HTMLInputElement).checked });
    });

  // Terminal-font selectors. On change, update the live hint with
  // the catalogue's one-liner about the picked font and apply the
  // pref — terminal.ts's applyPrefs re-resolves the family from the
  // device-appropriate id so the live xterm flips fonts immediately
  // without a page reload.
  const updateFontHint = (target: "Desktop" | "Mobile", id: string) => {
    const hintEl = sec.querySelector<HTMLElement>(
      `[data-role=terminalFont${target}-hint]`);
    if (!hintEl) return;
    const font = TERMINAL_FONTS.find(f => f.id === id);
    hintEl.textContent = font?.hint ?? "";
  };
  updateFontHint("Desktop", prefs.terminalFontDesktop);
  updateFontHint("Mobile",  prefs.terminalFontMobile);

  sec.querySelector<HTMLSelectElement>("select[name=terminalFontDesktop]")!
    .addEventListener("change", (e) => {
      const id = (e.target as HTMLSelectElement).value;
      updateFontHint("Desktop", id);
      apply({ ...working, terminalFontDesktop: id });
    });
  sec.querySelector<HTMLSelectElement>("select[name=terminalFontMobile]")!
    .addEventListener("change", (e) => {
      const id = (e.target as HTMLSelectElement).value;
      updateFontHint("Mobile", id);
      apply({ ...working, terminalFontMobile: id });
    });

  sec.querySelector<HTMLButtonElement>("[data-role=reset]")!
    .addEventListener("click", () => {
      apply({ ...DEFAULT_PREFS });
      // Refresh inputs to reflect defaults
      closeSettings();
      setTimeout(() => openSettings(opts), 50);
    });

  return sec;
}

// ─── Two-factor (TOTP) section ─────────────────────────────────────────

function buildTwoFactorSection(): HTMLElement {
  const sec = document.createElement("section");
  sec.className = "modal__section";
  sec.innerHTML = `
    <h2 class="modal__section-title">two-factor</h2>
    <div class="totp-status" data-role="totp-status-text">loading…</div>
    <div data-role="totp-actions"></div>
  `;
  const statusEl = sec.querySelector<HTMLElement>("[data-role=totp-status-text]")!;
  const actions  = sec.querySelector<HTMLElement>("[data-role=totp-actions]")!;
  _wireTotpUi(statusEl, actions);
  return sec;
}

// ─── TOTP enrollment + disable (helpers used by the section above) ─────

async function _wireTotpUi(statusEl: HTMLElement, actions: HTMLElement): Promise<void> {
  const refresh = async () => {
    actions.innerHTML = "";
    let s: { otp_enrolled?: boolean };
    try {
      const r = await fetch("/api/auth/status", { credentials: "same-origin" });
      s = await r.json();
    } catch {
      statusEl.textContent = "could not query 2FA status";
      return;
    }
    if (s.otp_enrolled) {
      statusEl.textContent = "enrolled — required at next login";
      statusEl.className = "totp-status totp-status--on";
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn btn--ghost";
      btn.textContent = "Disable two-factor";
      btn.addEventListener("click", () => _renderDisableForm(actions, refresh));
      actions.append(btn);
    } else {
      statusEl.textContent = "disabled — password-only login";
      statusEl.className = "totp-status";
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn btn--primary";
      btn.textContent = "Set up two-factor";
      btn.addEventListener("click", () => _renderEnrollForm(actions, refresh));
      actions.append(btn);
    }
  };
  await refresh();
}

/** Step 1 of enrollment: ask for the current password, fetch a fresh
 * secret + provisioning URI, render the QR + confirmation input. */
function _renderEnrollForm(host: HTMLElement, done: () => void): void {
  host.innerHTML = "";
  const form = document.createElement("div");
  form.className = "totp-enroll";
  form.innerHTML = `
    <div class="totp-enroll__row">
      <input type="password" class="input" placeholder="current password" data-role="cur"/>
      <button type="button" class="btn btn--primary" data-role="gen">Generate code</button>
    </div>
    <div class="totp-enroll__qr" data-role="qr" hidden></div>
    <div class="totp-enroll__row" data-role="confirm-row" hidden>
      <input type="text" class="input" placeholder="6-digit code from authenticator" inputmode="numeric" maxlength="8" data-role="code"/>
      <button type="button" class="btn btn--primary" data-role="confirm">Enable</button>
    </div>
    <div class="modal__status" data-role="enroll-status"></div>
  `;
  host.append(form);

  const cur = form.querySelector<HTMLInputElement>("[data-role=cur]")!;
  const gen = form.querySelector<HTMLButtonElement>("[data-role=gen]")!;
  const qr = form.querySelector<HTMLElement>("[data-role=qr]")!;
  const confirmRow = form.querySelector<HTMLElement>("[data-role=confirm-row]")!;
  const code = form.querySelector<HTMLInputElement>("[data-role=code]")!;
  const confirm = form.querySelector<HTMLButtonElement>("[data-role=confirm]")!;
  const stat = form.querySelector<HTMLElement>("[data-role=enroll-status]")!;

  let pendingSecret = "";

  gen.addEventListener("click", async () => {
    stat.textContent = "";
    stat.classList.remove("modal__status--error");
    if (!cur.value) {
      stat.textContent = "current password required";
      stat.classList.add("modal__status--error");
      return;
    }
    gen.disabled = true;
    try {
      const res = await fetch("/api/auth/totp/enroll", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-Requested-By": "ccpipe" },
        body: JSON.stringify({ currentPassword: cur.value }),
      });
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d.detail || `status ${res.status}`);
      }
      const data = await res.json();
      pendingSecret = data.secret;
      // QR is rendered server-side as SVG so the otpauth:// URI
      // (which embeds the secret) never reaches a third party.
      // Parse as XML and attach the resulting element rather than
      // assigning to innerHTML — defence-in-depth against a future
      // qrcode-lib change that embeds attacker-influenced text
      // (e.g. the username appearing in a <title>/<desc>) into the
      // SVG body.
      qr.innerHTML = "";
      const qrWrap = document.createElement("div");
      qrWrap.className = "totp-enroll__qr-image";
      const svgText = (data.qr_svg ?? "") as string;
      if (svgText) {
        const parsed = new DOMParser().parseFromString(svgText, "image/svg+xml");
        const svgEl = parsed.documentElement;
        // DOMParser surfaces a <parsererror> element if the XML was
        // malformed; in that case we just skip the QR (the manual
        // secret fallback below still renders).
        if (svgEl && svgEl.nodeName.toLowerCase() === "svg") {
          qrWrap.appendChild(document.importNode(svgEl, true));
        }
      }
      qr.append(qrWrap);
      const fallback = document.createElement("div");
      fallback.className = "totp-enroll__secret";
      fallback.textContent = "or enter manually: ";
      const codeEl = document.createElement("code");
      codeEl.textContent = data.secret;
      fallback.append(codeEl);
      qr.append(fallback);
      qr.hidden = false;
      confirmRow.hidden = false;
      code.focus();
    } catch (e) {
      stat.textContent = (e as Error).message;
      stat.classList.add("modal__status--error");
    } finally {
      gen.disabled = false;
    }
  });

  confirm.addEventListener("click", async () => {
    stat.textContent = "";
    stat.classList.remove("modal__status--error");
    if (!pendingSecret) {
      stat.textContent = "generate a code first";
      stat.classList.add("modal__status--error");
      return;
    }
    if (!/^[0-9]{6,8}$/.test(code.value)) {
      stat.textContent = "code must be 6 digits";
      stat.classList.add("modal__status--error");
      return;
    }
    confirm.disabled = true;
    try {
      const res = await fetch("/api/auth/totp/confirm", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-Requested-By": "ccpipe" },
        // currentPassword is required server-side so a stolen session
        // can't persist an attacker-chosen TOTP secret and lock the
        // legitimate user out. We still have it from the enroll step.
        body: JSON.stringify({
          currentPassword: cur.value,
          secret: pendingSecret,
          code: code.value,
        }),
      });
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d.detail || `status ${res.status}`);
      }
      stat.textContent = "two-factor enabled";
      setTimeout(done, 600);
    } catch (e) {
      stat.textContent = (e as Error).message;
      stat.classList.add("modal__status--error");
    } finally {
      confirm.disabled = false;
    }
  });
}

function _renderDisableForm(host: HTMLElement, done: () => void): void {
  host.innerHTML = "";
  const form = document.createElement("div");
  form.className = "totp-enroll";
  form.innerHTML = `
    <div class="totp-enroll__row">
      <input type="password" class="input" placeholder="current password" data-role="cur"/>
      <input type="text" class="input" placeholder="current 6-digit code" inputmode="numeric" maxlength="8" data-role="code"/>
      <button type="button" class="btn btn--ghost" data-role="disable">Disable</button>
    </div>
    <div class="modal__status" data-role="dis-status"></div>
  `;
  host.append(form);
  const cur = form.querySelector<HTMLInputElement>("[data-role=cur]")!;
  const code = form.querySelector<HTMLInputElement>("[data-role=code]")!;
  const btn = form.querySelector<HTMLButtonElement>("[data-role=disable]")!;
  const stat = form.querySelector<HTMLElement>("[data-role=dis-status]")!;
  btn.addEventListener("click", async () => {
    stat.textContent = "";
    stat.classList.remove("modal__status--error");
    if (!cur.value || !/^[0-9]{6,8}$/.test(code.value)) {
      stat.textContent = "password and code required";
      stat.classList.add("modal__status--error");
      return;
    }
    btn.disabled = true;
    try {
      const res = await fetch("/api/auth/totp/disable", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-Requested-By": "ccpipe" },
        body: JSON.stringify({ currentPassword: cur.value, code: code.value }),
      });
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d.detail || `status ${res.status}`);
      }
      stat.textContent = "two-factor disabled";
      setTimeout(done, 600);
    } catch (e) {
      stat.textContent = (e as Error).message;
      stat.classList.add("modal__status--error");
    } finally {
      btn.disabled = false;
    }
  });
}

// ─── About footer ───────────────────────────────────────────────────────

function buildAboutFooter(): HTMLElement {
  const f = document.createElement("div");
  f.className = "modal__footer";
  f.innerHTML = `
    <span class="wordmark small">cc<span class="dot"></span>pipe</span>
    <span class="modal__footer-meta">v${VERSION}</span>
  `;
  return f;
}
