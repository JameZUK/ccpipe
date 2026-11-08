// Push notifications on this device: what needs you (attention.ts) reaches
// it when no ccpipe page is on its screen, even with ccpipe closed.
//
// Turning them on asks the browser's permission, registers push-sw.js (a
// service worker that only shows notifications and opens the session you
// tap; it never handles the page's requests) and subscribes with the
// server's key. The server sends through the browser maker's push service,
// encrypted for this device (backend webpush.py).
//
// iPad and iPhone: only ccpipe added to the Home Screen can have them.

import { apiJson } from "./api";
import { deviceId, type Level } from "./attention";

/** `open`: also while ccpipe is on this device's screen, at once (an iPad:
 * the notification's sound, as the page's own stops the music there). */
export type PushPrefs = { ask: boolean; done: boolean; details: boolean; open: boolean };
export type PushDevice = {
  id: string; label: string; device: string; host: string;
  created: number; ok_at: number; prefs: PushPrefs;
};
export type PushState = { key: string; devices: PushDevice[]; push_wait_s: number };

const SW_URL = "/push-sw.js";
// What this device has push on for, so the page doesn't also show its own
// notification for the same thing (notifications.ts).
const COVER_KEY = "ccpipe.pushCovers";

const APPLE_TOUCH = /iPad|iPhone|iPod/.test(navigator.userAgent)
  || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
const STANDALONE = (() => {
  try {
    return matchMedia("(display-mode: standalone)").matches
      || (navigator as Navigator & { standalone?: boolean }).standalone === true;
  } catch { return false; }
})();

/** Whether this browser can have push, and if not, why not (in words). */
export function pushSupport(): { ok: true } | { ok: false; why: string } {
  if (!window.isSecureContext) return { ok: false, why: "Push notifications need ccpipe over HTTPS." };
  const have = "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
  if (!have) {
    if (APPLE_TOUCH && !STANDALONE) {
      return { ok: false, why: "On an iPad or iPhone, add ccpipe to the Home Screen (Share → Add to Home Screen) and turn them on there." };
    }
    return { ok: false, why: "This browser can't receive push notifications." };
  }
  return { ok: true };
}

export function pushCovers(level: Level): boolean {
  try { return JSON.parse(localStorage.getItem(COVER_KEY) || "{}")?.[level] === true; } catch { return false; }
}

function setCovers(prefs: PushPrefs | null): void {
  try {
    if (prefs) localStorage.setItem(COVER_KEY, JSON.stringify({ ask: prefs.ask, done: prefs.done }));
    else localStorage.removeItem(COVER_KEY);
  } catch { /* private mode */ }
}

/** "Firefox on Android", "iPad", "Chrome on Linux": how the device list
 * names this one. */
export function deviceLabel(): string {
  const ua = navigator.userAgent;
  const os = /iPad/.test(ua) || (APPLE_TOUCH && /Macintosh/.test(ua)) ? "iPad"
    : /iPhone/.test(ua) ? "iPhone"
    : /Android/.test(ua) ? "Android"
    : /Windows/.test(ua) ? "Windows"
    : /Mac OS X/.test(ua) ? "Mac"
    : /Linux/.test(ua) ? "Linux" : "";
  if (os === "iPad" || os === "iPhone") return os;
  const browser = /Edg\//.test(ua) ? "Edge" : /Firefox\//.test(ua) ? "Firefox"
    : /Chrome\//.test(ua) ? "Chrome" : /Safari\//.test(ua) ? "Safari" : "Browser";
  return os ? `${browser} on ${os}` : browser;
}

function keyBytes(b64: string): Uint8Array {
  const s = atob(b64.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (b64.length % 4)) % 4));
  return Uint8Array.from(s, (c) => c.charCodeAt(0));
}

function sameKey(a: ArrayBuffer | null | undefined, b: Uint8Array): boolean {
  if (!a) return false;
  const x = new Uint8Array(a);
  return x.length === b.length && x.every((v, i) => v === b[i]);
}

async function registration(): Promise<ServiceWorkerRegistration | null> {
  if (!("serviceWorker" in navigator)) return null;
  try {
    return (await navigator.serviceWorker.getRegistration("/")) ?? null;
  } catch { return null; }
}

/** This device's subscription, if push is on here. */
export async function currentSubscription(): Promise<PushSubscription | null> {
  const reg = await registration();
  if (!reg || !reg.active || !String(reg.active.scriptURL).endsWith(SW_URL)) return null;
  try { return await reg.pushManager.getSubscription(); } catch { return null; }
}

export async function pushState(): Promise<PushState> {
  return apiJson<PushState>("/api/push");
}

/** This device's subscription and the server's record of it. A device the
 * server no longer has (its push service said it's gone, or it was
 * removed from another device's Settings) is turned off here too, so its
 * open tabs notify again and Settings says it's off. If the server can't
 * be asked, nothing changes. */
export async function pushStatus(): Promise<{ sub: PushSubscription | null; device: PushDevice | null }> {
  const sub = await currentSubscription();
  if (!sub) {
    setCovers(null);
    return { sub: null, device: null };
  }
  let device: PushDevice | null;
  try {
    ({ device } = await apiJson<{ device: PushDevice | null }>("/api/push/status", {
      method: "POST", body: JSON.stringify({ endpoint: sub.endpoint }),
    }));
  } catch {
    return { sub, device: null };
  }
  if (!device) {
    setCovers(null);
    await sub.unsubscribe().catch(() => false);
    return { sub: null, device: null };
  }
  setCovers(device.prefs);
  return { sub, device };
}

/** Turn push on here. Call it straight from a tap: the permission prompt
 * must be the first thing it waits for (Safari's rule). */
export async function enablePush(prefs: PushPrefs): Promise<PushDevice> {
  const perm = await Notification.requestPermission();
  if (perm !== "granted") {
    throw new Error(perm === "denied"
      ? "Notifications are blocked for ccpipe in this browser's settings."
      : "Notifications weren't allowed.");
  }
  const reg = await navigator.serviceWorker.register(SW_URL, { scope: "/" });
  await navigator.serviceWorker.ready;
  const { key } = await pushState();
  const want = keyBytes(key);
  let sub = await reg.pushManager.getSubscription();
  if (sub && !sameKey(sub.options.applicationServerKey, want)) {
    // Subscribed with an older key of this server's: start again.
    await sub.unsubscribe().catch(() => false);
    sub = null;
  }
  sub ??= await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: want as BufferSource });
  const json = sub.toJSON();
  const dev = await apiJson<PushDevice>("/api/push/subscribe", {
    method: "POST",
    body: JSON.stringify({ endpoint: json.endpoint, keys: json.keys, device: deviceId(),
      label: deviceLabel(), prefs }),
  });
  setCovers(dev.prefs);
  return dev;
}

export async function disablePush(): Promise<void> {
  const sub = await currentSubscription();
  setCovers(null);
  if (!sub) return;
  await apiJson("/api/push/unsubscribe", { method: "POST", body: JSON.stringify({ endpoint: sub.endpoint }) })
    .catch(() => {});
  await sub.unsubscribe().catch(() => false);
}

export async function setPushPrefs(prefs: Partial<PushPrefs>): Promise<PushDevice | null> {
  const sub = await currentSubscription();
  if (!sub) return null;
  const dev = await apiJson<PushDevice>("/api/push/prefs", {
    method: "POST", body: JSON.stringify({ endpoint: sub.endpoint, prefs }),
  });
  setCovers(dev.prefs);
  return dev;
}

export async function testPush(): Promise<{ ok: boolean; status: number; detail: string }> {
  const sub = await currentSubscription();
  if (!sub) return { ok: false, status: 0, detail: "push isn't on for this device" };
  return apiJson("/api/push/test", { method: "POST", body: JSON.stringify({ endpoint: sub.endpoint }) });
}

export async function forgetDevice(id: string): Promise<void> {
  await apiJson("/api/push/unsubscribe", { method: "POST", body: JSON.stringify({ id }) });
}

export async function setPushWait(seconds: number): Promise<void> {
  await apiJson("/api/push/config", { method: "POST", body: JSON.stringify({ push_wait_s: seconds }) });
}

/** A tapped notification asks an open page to show its session. */
export function onNotificationOpen(open: (session: string) => void): () => void {
  if (!("serviceWorker" in navigator)) return () => {};
  const fn = (e: MessageEvent) => {
    const d = e.data as { type?: string; session?: unknown } | null;
    if (d?.type === "ccpipe-open" && typeof d.session === "string") open(d.session);
  };
  navigator.serviceWorker.addEventListener("message", fn);
  // Messages wait until the page says it's listening.
  try { navigator.serviceWorker.startMessages(); } catch { /* older browser: automatic */ }
  return () => navigator.serviceWorker.removeEventListener("message", fn);
}

/** Close this device's notifications that no longer need you: the ones for
 * a session now on screen, and any whose item is gone from `keep`. */
export async function tidyNotifications(keep: Array<{ session: string; level: string }>,
                                        showing: string | null = null): Promise<void> {
  const reg = await registration();
  if (!reg?.getNotifications) return;
  try {
    const live = new Set(keep.map((k) => `ccpipe-${k.level}-${k.session}`));
    for (const n of await reg.getNotifications()) {
      const tag = n.tag || "";
      if (!tag.startsWith("ccpipe-") || tag === "ccpipe-test" || tag === "ccpipe-response") continue;
      const session = (n.data as { session?: string } | null)?.session ?? "";
      if (!live.has(tag) || (showing && session === showing)) n.close();
    }
  } catch { /* not supported here */ }
}
