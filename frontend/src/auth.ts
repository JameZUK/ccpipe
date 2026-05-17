// Cookie-based auth client.

export interface AuthStatus {
  required: boolean;
  authenticated: boolean;
  username?: string | null;
}

export async function fetchAuthStatus(): Promise<AuthStatus> {
  const res = await fetch("/api/auth/status", { credentials: "same-origin" });
  if (!res.ok) throw new Error(`auth status: ${res.status}`);
  return res.json();
}

export async function login(username: string, password: string): Promise<AuthStatus> {
  const res = await fetch("/api/auth/login", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-Requested-By": "ccpipe" },
    body: JSON.stringify({ username, password }),
  });
  if (res.status === 401) {
    return { required: true, authenticated: false };
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

  const head = document.createElement("div");
  head.className = "frame__head";
  const word = document.createElement("div");
  word.className = "wordmark huge";
  word.innerHTML = `cc<span class="dot"></span>pipe`;
  const tagline = document.createElement("div");
  tagline.className = "tagline";
  tagline.textContent = "remote shell · claude code";
  head.append(word, tagline);

  const wrap = document.createElement("div");
  wrap.className = "login";

  const legend = document.createElement("div");
  legend.className = "login__legend";
  legend.textContent = "Sign in";

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

  const err = document.createElement("div");
  err.className = "error";

  const hint = document.createElement("div");
  hint.className = "login__hint";
  hint.textContent = "first run? credentials are at ~/.local/state/ccpipe/credentials";

  wrap.append(legend, form, err, hint);
  inner.append(head, wrap);
  frame.append(inner);
  root.append(frame);

  setTimeout(() => userInput.focus(), 50);

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    err.textContent = "";
    btn.disabled = true;
    try {
      const status = await login(userInput.value, passInput.value);
      if (status.authenticated) {
        onSuccess();
      } else {
        err.textContent = "invalid credentials";
        passInput.select();
      }
    } catch (e) {
      err.textContent = (e as Error).message;
    } finally {
      btn.disabled = false;
    }
  });
}
