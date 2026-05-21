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

export interface AuthStatus {
  required: boolean;
  authenticated: boolean;
  username?: string | null;
  // Retained for backward compat with the wire model only — the server
  // no longer sets it, and the client never branches on it.
  otp_required?: boolean;
  otp_enrolled?: boolean;
}

export async function fetchAuthStatus(): Promise<AuthStatus> {
  const res = await fetch("/api/auth/status", { credentials: "same-origin" });
  if (!res.ok) throw new Error(`auth status: ${res.status}`);
  return res.json();
}

export async function login(
  username: string, password: string, code?: string,
): Promise<AuthStatus & { error?: string }> {
  const body: Record<string, string> = { username, password };
  if (code) body.code = code;
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
  // that breathes and rotates slowly. Pure SVG + CSS, no extra JS or
  // requestAnimationFrame loop.
  const head = document.createElement("div");
  head.className = "login__head";

  const mark = document.createElement("div");
  mark.className = "login-mark";
  mark.setAttribute("aria-hidden", "true");
  // Each petal uses the SAME path (a vertical diamond centred on the
  // origin). The per-petal rotation is applied in CSS via a custom
  // property so it can compose with the breathing scale animation —
  // a SVG `transform=` attribute would be overridden by CSS transform.
  mark.innerHTML = `
    <svg class="login-mark__svg" viewBox="-40 -40 80 80">
      <g class="login-mark__spark">
        <path class="login-mark__petal login-mark__petal--a" d="M0 -34 L4 0 L0 34 L-4 0 Z"/>
        <path class="login-mark__petal login-mark__petal--b" d="M0 -34 L4 0 L0 34 L-4 0 Z"/>
        <path class="login-mark__petal login-mark__petal--c" d="M0 -34 L4 0 L0 34 L-4 0 Z"/>
        <path class="login-mark__petal login-mark__petal--d" d="M0 -34 L4 0 L0 34 L-4 0 Z"/>
      </g>
      <circle class="login-mark__core" cx="0" cy="0" r="2.4"/>
    </svg>
  `;

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

  wrap.append(legend, form, otpForm, err, hint);
  inner.append(head, wrap);
  frame.append(inner);
  root.append(frame);

  setTimeout(() => userInput.focus(), 50);

  // Step 1 → step 2 transition. NO API call here.
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    err.textContent = "";
    form.hidden = true;
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
        onSuccess();
        return;
      }
      // Single uniform error path — no per-field reveal. Bounce the
      // UI back to step 1 so the operator re-enters from scratch;
      // clear the code so a typo isn't retried unintentionally.
      err.textContent = status.error || "invalid credentials";
      otpForm.hidden = true;
      form.hidden = false;
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
    legend.textContent = "Sign in";
    hint.textContent = "";
    hint.hidden = true;
    err.textContent = "";
    otpInput.value = "";
    setTimeout(() => userInput.focus(), 50);
  });
}
