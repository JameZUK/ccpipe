// Cookie-based auth client.
//
// Wire flow: ONE POST. The client always submits {username, password,
// code?} together and the server returns an identical 401 "invalid
// credentials" if any part is wrong (or if a TOTP-enrolled account
// omitted the code) — so the response distinguishes only "authenticated"
// vs "not", with no positive password-correctness signal before the
// code is checked. Accounts without TOTP can leave `code` blank.
//
// UI flow: TWO screens. Step 1 collects username + password and is
// purely client-side (no API call); clicking Sign in advances to a
// dedicated step 2 that asks for the 6-digit code. Step 2 fires the
// single POST. Users without TOTP just leave the code blank and submit.
// This keeps the dedicated "enter your code" surface the operator
// prefers while preserving the single-call semantics that don't leak
// the password-correctness oracle.
//
// Passkeys (passkey.ts): "Sign in with a passkey" (or picking one from
// the username field's autofill) posts the assertion alone. If the
// account wants more than the passkey (Settings → Account → ways to sign
// in), the reply's `need` says what — only ever after a passkey has
// verified — and a third screen asks for just that.
import { sparkMark } from "./brand";
import { ICON_PASSKEY, iconNode } from "./icons";
import { fetchSignInOptions, getAssertion, passkeyAutofillAvailable, passkeysSupported } from "./passkey";


export interface AuthStatus {
  required: boolean;
  authenticated: boolean;
  username?: string | null;
  // Retained for backward compat with the wire model only — the server
  // no longer sets it, and the client never branches on it.
  otp_enrolled?: boolean;
  /** After a passkey verified: further factors that would finish it,
   * as alternatives — [["password"], ["code"]] = either one. */
  need?: string[][] | null;
}

export interface LoginBody {
  username?: string;
  password?: string;
  code?: string;
  /** An assertion from passkeyAssertion(). */
  passkey?: object;
}

export async function fetchAuthStatus(): Promise<AuthStatus> {
  const res = await fetch("/api/auth/status", { credentials: "same-origin" });
  if (!res.ok) throw new Error(`auth status: ${res.status}`);
  return res.json();
}

export async function login(
  username: string, password: string, code?: string,
): Promise<AuthStatus & { error?: string }> {
  return loginWith({ username, password, code: code || undefined });
}

export async function loginWith(body: LoginBody): Promise<AuthStatus & { error?: string }> {
  const res = await fetch("/api/auth/login", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-Requested-By": "ccpipe" },
    body: JSON.stringify(body),
  });
  if (res.status === 401) {
    const detail = await res.json().catch(() => ({}));
    return { required: true, authenticated: false, error: detail.detail || "invalid credentials" };
  }
  if (res.status === 429) {
    return { required: true, authenticated: false, error: "too many attempts, try again in a minute" };
  }
  if (!res.ok) throw new Error(`login: ${res.status}`);
  return res.json();
}

export async function logout(): Promise<void> {
  await fetch("/api/auth/logout", {
    method: "POST",
    credentials: "same-origin",
    headers: { "X-Requested-By": "ccpipe" },
  });
}

/** Invalidate every session on every device (password unchanged). */
export async function logoutEverywhere(): Promise<void> {
  const res = await fetch("/api/auth/logout-all", {
    method: "POST",
    credentials: "same-origin",
    headers: { "X-Requested-By": "ccpipe" },
  });
  if (!res.ok) throw new Error(`sign out everywhere: ${res.status}`);
}

export function isSecureContext(): boolean {
  // True for https:// and http://localhost. getUserMedia requires this.
  return window.isSecureContext;
}

export async function changeCredentials(body: {
  currentPassword: string;
  newUsername?: string;
  newPassword?: string;
  // Required by the backend when TOTP is enrolled (H1). The settings
  // UI reveals the input only when /api/auth/status reports otp_enrolled.
  code?: string;
}): Promise<{ updated: true } | { error: string }> {
  const res = await fetch("/api/auth/credentials", {
    method: "POST",
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      "X-Requested-By": "ccpipe",
    },
    body: JSON.stringify(body),
  });
  if (res.status === 200) return { updated: true };
  const detail = await res.json().catch(() => ({}));
  return { error: detail.detail || `status ${res.status}` };
}

export function renderLogin(root: HTMLElement, onSuccess: () => void): void {
  root.innerHTML = "";
  const frame = document.createElement("div");
  frame.className = "frame";

  const inner = document.createElement("div");
  inner.className = "frame__inner";

  // Minimal head: just the animated mark + a small lower-case wordmark.
  // Deliberately no descriptive tagline ("remote shell" / "claude code")
  // — the login screen is the most-scraped surface, so we don't tell
  // unauthenticated visitors what's behind it.
  //
  // The mark is a four-petal "spark" — two crossed diamond shapes —
  // that breathes and rotates slowly (brand.ts; also on the session picker).
  const head = document.createElement("div");
  head.className = "login__head";

  const mark = sparkMark("spark--login");

  const word = document.createElement("div");
  word.className = "wordmark small login__wordmark";
  word.innerHTML = `cc<span class="dot"></span>pipe`;

  head.append(mark, word);

  const wrap = document.createElement("div");
  wrap.className = "login";

  const legend = document.createElement("div");
  legend.className = "login__legend";
  legend.textContent = "Sign in";

  // ── Step 1: username + password (client-side only) ──────────────
  // Submitting this form does NOT hit the server — it just transitions
  // the UI to step 2. The server only sees a single POST once the user
  // has also entered (or skipped) the code, which is what closes the
  // password-correctness oracle.
  const form = document.createElement("form");
  form.className = "login__form";

  const userInput = document.createElement("input");
  userInput.type = "text";
  userInput.name = "username";
  userInput.placeholder = "username";
  userInput.autocomplete = "username";
  userInput.spellcheck = false;
  userInput.autocapitalize = "none";
  userInput.required = true;

  const passInput = document.createElement("input");
  passInput.type = "password";
  passInput.name = "password";
  passInput.placeholder = "password";
  passInput.autocomplete = "current-password";
  passInput.spellcheck = false;
  passInput.required = true;

  const submitRow = document.createElement("div");
  submitRow.className = "login__submit";
  const btn = document.createElement("button");
  btn.type = "submit";
  btn.className = "btn btn--primary";
  btn.textContent = "Continue";
  submitRow.append(btn);

  form.append(userInput, passInput, submitRow);

  // ── Passkey: a button (and the username field's autofill) ────────
  const pkWrap = document.createElement("div");
  pkWrap.className = "login__passkey";
  pkWrap.hidden = !passkeysSupported();
  const orEl = document.createElement("div");
  orEl.className = "login__or";
  orEl.textContent = "or";
  const pkBtn = document.createElement("button");
  pkBtn.type = "button";
  pkBtn.className = "btn btn--ghost login__passkey-btn";
  pkBtn.append(iconNode(ICON_PASSKEY), document.createTextNode("Sign in with a passkey"));
  pkWrap.append(orEl, pkBtn);

  // ── After a passkey, when the account wants more ─────────────────
  const finishForm = document.createElement("form");
  finishForm.className = "login__form";
  finishForm.hidden = true;
  const finPass = document.createElement("input");
  finPass.type = "password";
  finPass.name = "password";
  finPass.placeholder = "password";
  finPass.autocomplete = "current-password";
  finPass.spellcheck = false;
  const finCode = document.createElement("input");
  finCode.type = "text";
  finCode.name = "code";
  finCode.placeholder = "6-digit code";
  finCode.autocomplete = "one-time-code";
  finCode.inputMode = "numeric";
  finCode.maxLength = 8;
  finCode.pattern = "[0-9]*";
  const finRow = document.createElement("div");
  finRow.className = "login__submit";
  const finBack = document.createElement("button");
  finBack.type = "button";
  finBack.className = "btn btn--ghost";
  finBack.textContent = "back";
  const finBtn = document.createElement("button");
  finBtn.type = "submit";
  finBtn.className = "btn btn--primary";
  finBtn.textContent = "Sign in";
  finRow.append(finBack, finBtn);
  finishForm.append(finPass, finCode, finRow);

  // ── Step 2: TOTP code ───────────────────────────────────────────
  // Always rendered for everyone — we never ask the server pre-auth
  // whether TOTP is enrolled, so the UI can't branch on it. Users
  // without TOTP just leave the field blank and click Sign in.
  const otpForm = document.createElement("form");
  otpForm.className = "login__form";
  otpForm.hidden = true;

  const otpInput = document.createElement("input");
  otpInput.type = "text";
  otpInput.name = "code";
  otpInput.placeholder = "6-digit code";
  otpInput.autocomplete = "one-time-code";
  otpInput.spellcheck = false;
  otpInput.autocapitalize = "none";
  otpInput.inputMode = "numeric";
  otpInput.maxLength = 8;
  otpInput.pattern = "[0-9]*";
  // NOT required — accounts without TOTP submit an empty code.

  const otpSubmitRow = document.createElement("div");
  otpSubmitRow.className = "login__submit";
  const otpBtn = document.createElement("button");
  otpBtn.type = "submit";
  otpBtn.className = "btn btn--primary";
  otpBtn.textContent = "Sign in";
  const otpBackBtn = document.createElement("button");
  otpBackBtn.type = "button";
  otpBackBtn.className = "btn btn--ghost";
  otpBackBtn.textContent = "back";
  otpSubmitRow.append(otpBackBtn, otpBtn);

  otpForm.append(otpInput, otpSubmitRow);

  const err = document.createElement("div");
  err.className = "error";

  const hint = document.createElement("div");
  hint.className = "login__hint";
  hint.hidden = true;

  wrap.append(legend, form, pkWrap, otpForm, finishForm, err, hint);
  inner.append(head, wrap);
  frame.append(inner);
  root.append(frame);

  setTimeout(() => userInput.focus(), 50);

  // ── Passkey sign-in ──────────────────────────────────────────────
  let done = false;
  let need: string[][] = [];
  let autofill: AbortController | null = null;
  const stopAutofill = (): void => { autofill?.abort(); autofill = null; };
  const succeed = (): void => { done = true; stopAutofill(); onSuccess(); };
  const showStep1 = (message = ""): void => {
    form.hidden = false;
    pkWrap.hidden = !passkeysSupported();
    otpForm.hidden = true;
    finishForm.hidden = true;
    legend.textContent = "Sign in";
    hint.hidden = true;
    hint.textContent = "";
    err.textContent = message;
    otpInput.value = "";
    finPass.value = "";
    finCode.value = "";
  };
  const needWords = (n: string[][]): string => n
    .map((alt) => alt.map((f) => (f === "code" ? "the 6-digit code" : "your password")).join(" and "))
    .join(", or ");
  const showFinish = (n: string[][]): void => {
    need = n;
    const wants = new Set(n.flat());
    finPass.hidden = !wants.has("password");
    finCode.hidden = !wants.has("code");
    form.hidden = true;
    pkWrap.hidden = true;
    otpForm.hidden = true;
    finishForm.hidden = false;
    legend.textContent = "Passkey accepted";
    hint.textContent = `now enter ${needWords(n)}`;
    hint.hidden = false;
    err.textContent = "";
    setTimeout(() => (wants.has("password") ? finPass : finCode).focus(), 50);
  };
  const usePasskey = async (assertion: object): Promise<void> => {
    const status = await loginWith({ passkey: assertion });
    if (status.authenticated) { succeed(); return; }
    if (status.need?.length) { showFinish(status.need); return; }
    showStep1(status.error === "invalid credentials" ? "that passkey wasn't accepted" : status.error || "sign-in failed");
  };
  // Passkeys in the username field's suggestions (iOS / Android keyboards,
  // password managers). Its challenge lives 5 minutes, so renew it at 4.
  const startAutofill = async (): Promise<void> => {
    if (done || !(await passkeyAutofillAvailable())) return;
    stopAutofill();
    const ac = new AbortController();
    autofill = ac;
    userInput.autocomplete = "username webauthn";
    const renew = setTimeout(() => { if (autofill === ac) void startAutofill(); }, 240_000);
    try {
      const options = await fetchSignInOptions();
      if (autofill !== ac) return;
      const assertion = await getAssertion(options, { autofill: true, signal: ac.signal });
      if (assertion && autofill === ac) {
        autofill = null;
        await usePasskey(assertion);
      }
    } catch { /* a convenience only — the button still works */ } finally {
      clearTimeout(renew);
    }
  };
  // Options fetched ahead of the tap (Safari only opens the passkey
  // prompt as the direct result of one), renewed before they expire.
  let ready: { options: PublicKeyCredentialRequestOptions; at: number } | null = null;
  const prepare = async (): Promise<void> => {
    try { ready = { options: await fetchSignInOptions(), at: Date.now() }; } catch { ready = null; }
  };
  if (passkeysSupported()) {
    void prepare();
    const prepTimer = setInterval(() => { if (done) clearInterval(prepTimer); else void prepare(); }, 240_000);
  }
  pkBtn.addEventListener("click", async () => {
    const fresh = ready && Date.now() - ready.at < 240_000 ? ready.options : null;
    ready = null;
    stopAutofill();
    err.textContent = "";
    pkBtn.disabled = true;
    try {
      if (fresh) {
        const assertion = await getAssertion(fresh);
        if (assertion) await usePasskey(assertion);
      } else {
        // Nothing fetched yet (a very quick tap): fetching first can cost
        // the tap its say (Safari), so the prompt may not open. Browsers
        // that still allow it go ahead; otherwise ask for another tap.
        const assertion = await getAssertion(await fetchSignInOptions());
        if (assertion) await usePasskey(assertion);
        else err.textContent = "tap “Sign in with a passkey” again";
      }
    } catch (e) {
      err.textContent = (e as Error).message;
    } finally {
      pkBtn.disabled = false;
      if (!done) {
        void prepare();
        if (finishForm.hidden) void startAutofill();
      }
    }
  });
  finishForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const pw = finPass.hidden ? "" : finPass.value;
    const code = finCode.hidden ? "" : finCode.value.trim();
    const given = new Set([...(pw ? ["password"] : []), ...(code ? ["code"] : [])]);
    if (!need.some((alt) => alt.every((f) => given.has(f)))) {
      err.textContent = `enter ${needWords(need)}`;
      return;
    }
    finBtn.disabled = true;
    try {
      const status = await loginWith({ password: pw || undefined, code: code || undefined });
      if (status.authenticated) { succeed(); return; }
      if (status.need?.length) { showFinish(status.need); return; }
      // One try per passkey: start again from the passkey.
      showStep1(`${status.error || "invalid credentials"} — use the passkey again`);
      void startAutofill();
    } catch (e2) {
      err.textContent = (e2 as Error).message;
    } finally {
      finBtn.disabled = false;
    }
  });
  finBack.addEventListener("click", () => { showStep1(); void startAutofill(); });
  void startAutofill();

  // Step 1 → step 2 transition. NO API call here.
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    err.textContent = "";
    form.hidden = true;
    pkWrap.hidden = true;
    otpForm.hidden = false;
    legend.textContent = "Enter the 6-digit code";
    hint.textContent = "from your authenticator app";
    hint.hidden = false;
    setTimeout(() => otpInput.focus(), 50);
  });

  // Step 2 — the only request. Submits {username, password, code?} as
  // one shot and handles a single generic error path.
  otpForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    err.textContent = "";
    otpBtn.disabled = true;
    try {
      const code = otpInput.value.trim();
      const status = await login(userInput.value, passInput.value, code || undefined);
      if (status.authenticated) {
        succeed();
        return;
      }
      // Single uniform error path — no per-field reveal. Bounce the
      // UI back to step 1 so the operator re-enters from scratch;
      // clear the code so a typo isn't retried unintentionally.
      err.textContent = status.error || "invalid credentials";
      otpForm.hidden = true;
      form.hidden = false;
      pkWrap.hidden = !passkeysSupported();
      legend.textContent = "Sign in";
      hint.hidden = true;
      hint.textContent = "";
      otpInput.value = "";
      passInput.select();
    } catch (e) {
      err.textContent = (e as Error).message;
    } finally {
      otpBtn.disabled = false;
    }
  });

  // Back button: undo the client-side step transition (no API call).
  otpBackBtn.addEventListener("click", () => {
    otpForm.hidden = true;
    form.hidden = false;
    pkWrap.hidden = !passkeysSupported();
    legend.textContent = "Sign in";
    hint.textContent = "";
    hint.hidden = true;
    err.textContent = "";
    otpInput.value = "";
    setTimeout(() => userInput.focus(), 50);
  });
}
