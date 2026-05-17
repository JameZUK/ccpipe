// Settings modal. Three sections (Voice, Account, Display) plus an About
// footer. Modal overlay; Esc and click-outside both dismiss.
//
// Voice + TTS settings are persisted server-side via /api/tts/config so
// they apply across devices. Display preferences are local to this
// browser via localStorage (see display-prefs.ts).
//
// To open the modal, call openSettings({...}) from anywhere with access
// to the helpers it needs.

import { changeCredentials, logout as apiLogout } from "./auth";
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

  modal.append(
    buildHeader(),
    buildVoiceSection(),
    buildAccountSection(opts),
    buildDisplaySection(opts),
    buildAboutFooter(),
  );

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

  const apply = (current: DisplayPrefs) => {
    saveDisplayPrefs(current);
    opts.onDisplayPrefsChange(current);
  };

  const wireRange = (name: keyof DisplayPrefs, fmt: (n: number) => string) => {
    const input = sec.querySelector<HTMLInputElement>(`input[name=${name}]`)!;
    const label = sec.querySelector<HTMLElement>(`[data-role=${name}-value]`)!;
    input.addEventListener("input", () => {
      const next = { ...loadDisplayPrefs(), [name]: parseFloat(input.value) };
      label.textContent = fmt(next[name] as number);
      apply(next as DisplayPrefs);
    });
  };
  wireRange("fontSize", (n) => `${Math.round(n)}px`);
  wireRange("lineHeight", (n) => n.toFixed(2));
  wireRange("letterSpacing", (n) => `${n}px`);

  sec.querySelector<HTMLSelectElement>("select[name=cursorStyle]")!
    .addEventListener("change", (e) => {
      apply({ ...loadDisplayPrefs(), cursorStyle: (e.target as HTMLSelectElement).value as any });
    });
  sec.querySelector<HTMLInputElement>("input[name=cursorBlink]")!
    .addEventListener("change", (e) => {
      apply({ ...loadDisplayPrefs(), cursorBlink: (e.target as HTMLInputElement).checked });
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
