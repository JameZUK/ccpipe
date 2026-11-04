// Settings → Account → passkeys, and the ways to sign in.
//
// Every change re-checks the current password (and the 6-digit code when
// two-factor is on), as the server requires: a stolen session alone can't
// add its own passkey or loosen how the account signs in.
//
// Adding a passkey is two taps on purpose: "Continue" checks the password
// and fetches the options, then "Create passkey" calls the browser — Safari
// only opens the passkey prompt as the direct result of a tap.
import { ICON_PASSKEY, iconNode } from "./icons";
import {
  deviceName, finishRegistration, methodLabel, passkeysSupported, removePasskey,
  setSigninMethods, signinState, startRegistration,
  type Factor, type PasskeyInfo, type SigninMethod, type SigninState,
} from "./passkey";

/** Fired on document when TOTP is turned on or off (methods depend on it). */
export const SIGNIN_CHANGED = "ccpipe:signin-changed";

function el<K extends keyof HTMLElementTagNameMap>(tag: K, cls = "", text = ""): HTMLElementTagNameMap[K] {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text) e.textContent = text;
  return e;
}

function button(cls: string, text: string, icon?: string): HTMLButtonElement {
  const b = el("button", cls);
  b.type = "button";
  if (icon) b.append(iconNode(icon));
  b.append(el("span", "", text));
  return b;
}

function input(type: string, placeholder: string, autocomplete: string): HTMLInputElement {
  const i = el("input", "input");
  i.type = type;
  i.placeholder = placeholder;
  i.autocomplete = autocomplete as AutoFill;
  i.spellcheck = false;
  return i;
}

const day = (t: number): string =>
  new Date(t * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });

function ago(t: number): string {
  const s = Math.max(0, Date.now() / 1000 - t);
  if (s < 90) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  if (s < 86400 * 14) return `${Math.round(s / 86400)} days ago`;
  return day(t);
}

export function buildPasskeySection(): HTMLElement {
  const sec = el("section", "modal__section pk");
  const title = el("h2", "modal__section-title", "passkeys");
  const intro = el("p", "pk-intro",
    "Sign in with Face ID, Touch ID, Windows Hello, your phone, a security key or a password manager. "
    + "A passkey belongs to this address: one made here works only here.");
  const note = el("div", "pk-note");
  note.hidden = true;
  const list = el("div", "pk-list");
  const addBtn = button("btn btn--primary", "Add a passkey", ICON_PASSKEY);
  const addRow = el("div", "pk-actions");
  addRow.append(addBtn);
  const keyForm = el("div", "pk-form");
  keyForm.hidden = true;
  const methodsTitle = el("h3", "pk-subtitle", "ways to sign in");
  const methods = el("div", "pk-methods");
  const methodsHint = el("p", "pk-hint",
    "Any ticked way signs you in. For one that starts with a passkey, use “Sign in with a passkey” "
    + "on the sign-in page, and it asks for the rest. The password always works as a fallback "
    + "if no ticked way can be used.");
  const methodsForm = el("div", "pk-form");
  methodsForm.hidden = true;
  const status = el("div", "modal__status pk-status");
  sec.append(title, intro, note, list, addRow, keyForm, methodsTitle, methods, methodsHint, methodsForm, status);

  let state: SigninState | null = null;

  const setStatus = (msg: string, error = false): void => {
    status.textContent = msg;
    status.classList.toggle("modal__status--error", error);
  };

  const closeForm = (host: HTMLElement): void => {
    host.hidden = true;
    host.replaceChildren();
  };

  /** An inline "confirm with your password (+ code)" form in *host*. */
  const reauth = (host: HTMLElement, opts: {
    lead?: HTMLElement; go: string; run: (pw: string, code: string) => Promise<void>; onCancel?: () => void;
  }): void => {
    for (const f of [keyForm, methodsForm]) if (f !== host) closeForm(f);
    host.replaceChildren();
    host.hidden = false;
    const row = el("div", "totp-enroll__row");
    const pw = input("password", "current password", "current-password");
    const code = input("text", "6-digit code", "one-time-code");
    code.inputMode = "numeric";
    code.maxLength = 8;
    code.hidden = !state?.otp_enrolled;
    const go = button("btn btn--primary", opts.go);
    const cancel = button("btn btn--ghost", "cancel");
    if (opts.lead) host.append(opts.lead);
    row.append(pw, code, go, cancel);
    host.append(row);
    const submit = async (): Promise<void> => {
      if (!pw.value) { setStatus("current password required", true); pw.focus(); return; }
      if (!code.hidden && !/^[0-9]{6,8}$/.test(code.value.trim())) {
        setStatus("the 6-digit code is required too", true);
        code.focus();
        return;
      }
      go.disabled = true;
      setStatus("");
      try {
        await opts.run(pw.value, code.value.trim());
      } catch (e) {
        setStatus((e as Error).message, true);
      } finally {
        go.disabled = false;
      }
    };
    go.addEventListener("click", () => void submit());
    for (const i of [pw, code]) {
      i.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); void submit(); } });
    }
    cancel.addEventListener("click", () => { closeForm(host); setStatus(""); opts.onCancel?.(); });
    setTimeout(() => pw.focus(), 30);
  };

  const keyRow = (k: PasskeyInfo): HTMLElement => {
    const r = el("div", "pk-row");
    const icon = el("span", "pk-row__icon");
    icon.append(iconNode(ICON_PASSKEY));
    const main = el("div", "pk-row__main");
    const bits = [`added ${day(k.created)}`, k.last_used ? `used ${ago(k.last_used)}` : "not used yet"];
    if (k.backed_up) bits.push("synced");
    if (state?.rp_id && k.rp_id && k.rp_id !== state.rp_id) bits.push(`for ${k.rp_id}`);
    main.append(el("div", "pk-row__name", k.name), el("div", "pk-row__meta", bits.join(" · ")));
    const del = button("btn btn--ghost pk-row__remove", "remove");
    del.title = `Remove ${k.name}`;
    del.addEventListener("click", () => reauth(keyForm, {
      lead: el("div", "pk-form__lead", `Remove “${k.name}”? It stops working for ccpipe straight away.`),
      go: "Remove",
      run: async (pw, code) => {
        state = await removePasskey(k.id, pw, code);
        closeForm(keyForm);
        render();
        setStatus(`removed ${k.name}`);
      },
    }));
    r.append(icon, main, del);
    return r;
  };

  const why = (f: Factor[]): string => {
    const needs: string[] = [];
    if (f.includes("passkey") && !state?.passkeys.length) needs.push("add a passkey first");
    if (f.includes("code") && !state?.otp_enrolled) needs.push("set up two-factor first");
    return needs.join(" and ");
  };

  const chosen = (): Factor[][] => [...methods.querySelectorAll<HTMLInputElement>("input[type=checkbox]")]
    .filter((c) => c.checked && !c.disabled)
    .map((c) => (c.dataset.factors ?? "").split(",") as Factor[]);

  const askToSave = (): void => {
    if (!chosen().length) {
      setStatus("tick at least one way to sign in", true);
      closeForm(methodsForm);
      return;
    }
    setStatus("");
    reauth(methodsForm, {
      go: "Save",
      run: async (pw, code) => {
        state = await setSigninMethods(chosen(), pw, code);
        closeForm(methodsForm);
        render();
        setStatus("ways to sign in saved");
      },
      onCancel: () => render(),
    });
  };

  const methodRow = (m: SigninMethod): HTMLElement => {
    const label = el("label", `pk-method${m.usable ? "" : " is-unusable"}`);
    const cb = el("input", "checkbox");
    cb.type = "checkbox";
    cb.checked = m.enabled;
    cb.disabled = !m.usable;
    cb.dataset.factors = m.factors.join(",");
    cb.addEventListener("change", askToSave);
    label.append(cb, el("span", "pk-method__label", methodLabel(m.factors)));
    if (!m.usable) label.append(el("span", "pk-method__why", why(m.factors)));
    return label;
  };

  const render = (): void => {
    if (!state) return;
    let blocked = "";
    if (!window.isSecureContext) blocked = "Passkeys need ccpipe over HTTPS.";
    else if (!passkeysSupported()) blocked = "This browser can't use passkeys.";
    else if (!state.rp_id) blocked = "Passkeys need ccpipe at its host name over HTTPS, not an IP address.";
    note.textContent = blocked;
    note.hidden = !blocked;
    addBtn.disabled = !!blocked;
    list.replaceChildren(...(state.passkeys.length
      ? state.passkeys.map(keyRow)
      : [el("div", "pk-empty", "No passkeys yet.")]));
    methods.replaceChildren(...state.methods.map(methodRow));
  };

  addBtn.addEventListener("click", () => {
    const name = input("text", "name, e.g. iPad", "off");
    name.className = "input pk-form__name";
    name.value = deviceName();
    name.maxLength = 64;
    const lead = el("div", "pk-form__lead");
    lead.append(el("span", "", "Name it, then confirm with your password:"), name);
    reauth(keyForm, {
      lead,
      go: "Continue",
      run: async (pw, code) => {
        const options = await startRegistration(pw, code);
        // Second tap: the browser's passkey prompt must come from a tap.
        keyForm.replaceChildren();
        const make = button("btn btn--primary", "Create passkey", ICON_PASSKEY);
        const cancel = button("btn btn--ghost", "cancel");
        const row = el("div", "totp-enroll__row");
        row.append(make, cancel);
        keyForm.append(el("div", "pk-form__lead", "Now make it: your device will ask for your face, fingerprint or PIN."), row);
        cancel.addEventListener("click", () => { closeForm(keyForm); setStatus(""); });
        make.addEventListener("click", async () => {
          make.disabled = true;
          setStatus("");
          try {
            state = await finishRegistration(options, name.value.trim() || deviceName());
            closeForm(keyForm);
            render();
            setStatus("passkey added");
          } catch (e) {
            setStatus((e as Error).message, true);
            closeForm(keyForm);
          }
        });
        make.focus();
      },
    });
  });

  const load = async (): Promise<void> => {
    if (!sec.isConnected && state) { document.removeEventListener(SIGNIN_CHANGED, onChanged); return; }
    try {
      state = await signinState();
      render();
    } catch (e) {
      setStatus(`couldn't load passkeys: ${(e as Error).message}`, true);
    }
  };
  const onChanged = (): void => { void load(); };
  document.addEventListener(SIGNIN_CHANGED, onChanged);
  void load();
  return sec;
}
