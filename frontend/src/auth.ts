// Cookie-based auth client. Two-step flow when TOTP is enrolled:
//   1. POST {username, password}      → if otp_required, switch UI to
//                                        the code-entry step.
//   2. POST {username, password, code} → grants the session on success.

export interface AuthStatus {
  required: boolean;
  authenticated: boolean;
  username?: string | null;
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

  // ── Step 1: username + password ─────────────────────────────────
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
  btn.textContent = "Sign in";
  submitRow.append(btn);

  form.append(userInput, passInput, submitRow);

  // ── Step 2: TOTP code ───────────────────────────────────────────
  // Hidden until the password step indicates otp_required.
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
  otpInput.required = true;

  const otpSubmitRow = document.createElement("div");
  otpSubmitRow.className = "login__submit";
  const otpBtn = document.createElement("button");
  otpBtn.type = "submit";
  otpBtn.className = "btn btn--primary";
  otpBtn.textContent = "Verify";
  const otpBackBtn = document.createElement("button");
  otpBackBtn.type = "button";
  otpBackBtn.className = "btn btn--ghost";
  otpBackBtn.textContent = "back";
  otpSubmitRow.append(otpBackBtn, otpBtn);

  otpForm.append(otpInput, otpSubmitRow);

  const err = document.createElement("div");
  err.className = "error";

  // Hint is hidden by default; we only surface it on the TOTP step
  // ("from your authenticator app"). The pre-fix "first run? credentials
  // are in ~/.local/state/ccpipe/credentials" hint leaked the install
  // layout to anyone who hit the login page.
  const hint = document.createElement("div");
  hint.className = "login__hint";
  hint.hidden = true;

  wrap.append(legend, form, otpForm, err, hint);
  inner.append(head, wrap);
  frame.append(inner);
  root.append(frame);

  setTimeout(() => userInput.focus(), 50);

  // Step 1 submit — password.
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    err.textContent = "";
    btn.disabled = true;
    try {
      const status = await login(userInput.value, passInput.value);
      if (status.authenticated) {
        onSuccess();
        return;
      }
      if (status.otp_required) {
        // Switch to step 2.
        form.hidden = true;
        otpForm.hidden = false;
        legend.textContent = "Enter the 6-digit code";
        hint.textContent = "from your authenticator app";
        hint.hidden = false;
        setTimeout(() => otpInput.focus(), 50);
        return;
      }
      err.textContent = status.error || "invalid credentials";
      passInput.select();
    } catch (e) {
      err.textContent = (e as Error).message;
    } finally {
      btn.disabled = false;
    }
  });

  // Step 2 submit — TOTP code (re-submits username+password+code).
  otpForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    err.textContent = "";
    otpBtn.disabled = true;
    try {
      const status = await login(userInput.value, passInput.value, otpInput.value);
      if (status.authenticated) {
        onSuccess();
        return;
      }
      err.textContent = status.error || "invalid code";
      otpInput.select();
    } catch (e) {
      err.textContent = (e as Error).message;
    } finally {
      otpBtn.disabled = false;
    }
  });

  // Back button returns to step 1.
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
