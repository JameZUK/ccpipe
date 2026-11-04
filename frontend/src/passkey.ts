// Passkeys (WebAuthn) in the browser: the calls to navigator.credentials
// and the /api/auth/passkey/* + /api/auth/signin endpoints.
//
// The server (backend/ccpipe/passkeys.py) hands out options as JSON with
// binary fields in base64url; the browser API wants ArrayBuffers, and the
// results go back as JSON again. That conversion is done by hand here
// rather than with PublicKeyCredential.parse*OptionsFromJSON / toJSON,
// which older Safari / Firefox builds on the iPad and Android lack.
//
// Safari only lets navigator.credentials.get/create run as the direct
// result of a tap, so the options are fetched BEFORE the tap that uses
// them: fetchSignInOptions() / startRegistration() first, then
// getAssertion() / finishRegistration() call the browser API before
// awaiting anything.

export type Factor = "password" | "code" | "passkey";

export interface SigninMethod { factors: Factor[]; enabled: boolean; usable: boolean }
export interface PasskeyInfo {
  id: string; name: string; created: number; last_used: number; rp_id: string; backed_up: boolean;
}
export interface SigninState {
  otp_enrolled: boolean;
  /** The host passkeys are made for at this address; null = they can't be. */
  rp_id: string | null;
  passkeys: PasskeyInfo[];
  methods: SigninMethod[];
}

/** WebAuthn exists here: a secure context (HTTPS or localhost) with the API. */
export function passkeysSupported(): boolean {
  return window.isSecureContext && typeof window.PublicKeyCredential === "function"
    && !!navigator.credentials;
}

/** Passkeys can appear in the username field's autofill list. */
export async function passkeyAutofillAvailable(): Promise<boolean> {
  if (!passkeysSupported()) return false;
  const pkc = window.PublicKeyCredential as unknown as { isConditionalMediationAvailable?: () => Promise<boolean> };
  if (typeof pkc.isConditionalMediationAvailable !== "function") return false;
  try { return await pkc.isConditionalMediationAvailable(); } catch { return false; }
}

function b64uToBuf(s: string): ArrayBuffer {
  const b64 = s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4);
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out.buffer;
}

function bufToB64u(buf: ArrayBuffer): string {
  const bytes = new Uint8Array(buf);
  let bin = "";
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

type Json = Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any

async function post(url: string, body?: unknown): Promise<Json> {
  const res = await fetch(url, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-Requested-By": "ccpipe" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(res.status === 429 ? "too many attempts; try again in a minute"
      : (typeof data.detail === "string" ? data.detail : `status ${res.status}`));
  }
  return data;
}

const descriptors = (list: Json[] | undefined): PublicKeyCredentialDescriptor[] =>
  (list ?? []).map((c) => ({ type: "public-key", id: b64uToBuf(c.id), transports: c.transports }));

/** Why a WebAuthn call failed, in words; null = the person cancelled. */
function describe(e: unknown, making: boolean): string | null {
  const name = (e as DOMException)?.name;
  if (name === "NotAllowedError" || name === "AbortError") return null;
  if (name === "InvalidStateError" && making) return "this device already has a passkey for ccpipe";
  if (name === "SecurityError") return "passkeys don't work at this address";
  return (e as Error)?.message || String(e);
}

/** Fresh sign-in options (a one-use challenge, good for 5 minutes). */
export async function fetchSignInOptions(): Promise<PublicKeyCredentialRequestOptions> {
  const o = await post("/api/auth/passkey/options");
  return {
    challenge: b64uToBuf(o.challenge),
    rpId: o.rpId,
    timeout: o.timeout,
    userVerification: o.userVerification,
    allowCredentials: descriptors(o.allowCredentials),
  };
}

/** Ask for a passkey with *publicKey*. Resolves to the assertion for
 * /api/auth/login, or null when the person cancelled (or `signal`
 * aborted it). `autofill` waits for a pick from the username field's
 * suggestions instead of opening the passkey dialog. Calls the browser
 * API before its first await, so a tap's activation still counts. */
export async function getAssertion(
  publicKey: PublicKeyCredentialRequestOptions,
  opts: { autofill?: boolean; signal?: AbortSignal } = {},
): Promise<Json | null> {
  const pending = navigator.credentials.get({
    publicKey,
    signal: opts.signal,
    ...(opts.autofill ? { mediation: "conditional" as CredentialMediationRequirement } : {}),
  });
  let cred: PublicKeyCredential | null;
  try {
    cred = await pending as PublicKeyCredential | null;
  } catch (e) {
    const why = describe(e, false);
    if (why === null) return null;
    throw new Error(why);
  }
  if (!cred) return null;
  const r = cred.response as AuthenticatorAssertionResponse;
  return {
    id: cred.id,
    rawId: bufToB64u(cred.rawId),
    type: cred.type,
    response: {
      clientDataJSON: bufToB64u(r.clientDataJSON),
      authenticatorData: bufToB64u(r.authenticatorData),
      signature: bufToB64u(r.signature),
      userHandle: r.userHandle ? bufToB64u(r.userHandle) : null,
    },
    clientExtensionResults: cred.getClientExtensionResults?.() ?? {},
    authenticatorAttachment: cred.authenticatorAttachment ?? null,
  };
}

export async function signinState(): Promise<SigninState> {
  const res = await fetch("/api/auth/signin", { credentials: "same-origin" });
  if (!res.ok) throw new Error(`status ${res.status}`);
  return res.json();
}

/** Step 1 of adding a passkey: re-check the password (and code) and get
 * creation options. Step 2, finishRegistration(), must run from a tap. */
export async function startRegistration(currentPassword: string, code?: string): Promise<PublicKeyCredentialCreationOptions> {
  const o = await post("/api/auth/passkey/register/options", { currentPassword, code: code || undefined });
  return {
    challenge: b64uToBuf(o.challenge),
    rp: o.rp,
    user: { id: b64uToBuf(o.user.id), name: o.user.name, displayName: o.user.displayName },
    pubKeyCredParams: o.pubKeyCredParams,
    timeout: o.timeout,
    excludeCredentials: descriptors(o.excludeCredentials),
    authenticatorSelection: o.authenticatorSelection,
    attestation: o.attestation ?? "none",
  };
}

/** Step 2: make the passkey on this device and save it to the account. */
export async function finishRegistration(publicKey: PublicKeyCredentialCreationOptions, name: string): Promise<SigninState> {
  const pending = navigator.credentials.create({ publicKey });
  let cred: PublicKeyCredential | null;
  try {
    cred = await pending as PublicKeyCredential | null;
  } catch (e) {
    throw new Error(describe(e, true) ?? "cancelled — no passkey was made");
  }
  if (!cred) throw new Error("cancelled — no passkey was made");
  const r = cred.response as AuthenticatorAttestationResponse;
  return await post("/api/auth/passkey/register", {
    name,
    credential: {
      id: cred.id,
      rawId: bufToB64u(cred.rawId),
      type: cred.type,
      response: {
        clientDataJSON: bufToB64u(r.clientDataJSON),
        attestationObject: bufToB64u(r.attestationObject),
        transports: r.getTransports?.() ?? [],
      },
      clientExtensionResults: cred.getClientExtensionResults?.() ?? {},
      authenticatorAttachment: cred.authenticatorAttachment ?? null,
    },
  }) as SigninState;
}

export async function removePasskey(id: string, currentPassword: string, code?: string): Promise<SigninState> {
  return await post("/api/auth/passkey/delete", { id, currentPassword, code: code || undefined }) as SigninState;
}

export async function setSigninMethods(methods: Factor[][], currentPassword: string, code?: string): Promise<SigninState> {
  return await post("/api/auth/signin", { methods, currentPassword, code: code || undefined }) as SigninState;
}

const FACTOR_WORDS: Record<Factor, string> = {
  passkey: "passkey", password: "password", code: "code",
};

/** "Passkey + password + code" — always in that order. */
export function methodLabel(factors: Factor[]): string {
  const order: Factor[] = ["passkey", "password", "code"];
  const words = order.filter((f) => factors.includes(f)).map((f) => FACTOR_WORDS[f]);
  return words.join(" + ").replace(/^./, (c) => c.toUpperCase());
}

/** A name for a passkey made on this device, e.g. "iPad · Safari". */
export function deviceName(): string {
  const ua = navigator.userAgent;
  const device = /iPad|Macintosh.*Mobile/.test(ua) || (/Macintosh/.test(ua) && navigator.maxTouchPoints > 1) ? "iPad"
    : /iPhone/.test(ua) ? "iPhone"
      : /Android/.test(ua) ? "Android"
        : /Macintosh|Mac OS X/.test(ua) ? "Mac"
          : /Windows/.test(ua) ? "Windows"
            : /Linux/.test(ua) ? "Linux" : "this device";
  const browser = /Firefox\//.test(ua) ? "Firefox"
    : /Edg\//.test(ua) ? "Edge"
      : /Chrome\//.test(ua) ? "Chrome"
        : /Safari\//.test(ua) ? "Safari" : "";
  return browser ? `${device} · ${browser}` : device;
}
