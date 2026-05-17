// Lightweight Notification helper for "claude finished responding" pings
// while the tab is backgrounded. Permission is requested explicitly via
// requestPermission() (call from a user gesture); fire() only emits when
// the permission is already granted, so we never surprise the user with
// a notification they didn't opt into.

const LS_KEY = "ccpipe.notifyOnReply";

const supported = typeof window !== "undefined" && "Notification" in window;

export function notificationsSupported(): boolean { return supported; }

export function isEnabled(): boolean {
  if (!supported) return false;
  if (Notification.permission !== "granted") return false;
  try { return localStorage.getItem(LS_KEY) === "1"; } catch { return false; }
}

export function setEnabled(on: boolean): void {
  try { localStorage.setItem(LS_KEY, on ? "1" : "0"); } catch {}
}

/** Request browser permission. Must be called from a user gesture
 * (button click). Returns the resulting permission string. */
export async function requestPermission(): Promise<NotificationPermission> {
  if (!supported) return "denied";
  try {
    const p = await Notification.requestPermission();
    return p;
  } catch {
    return "denied";
  }
}

/** Fire a "response ready" notification IF:
 *   - the browser supports notifications,
 *   - the user has granted permission,
 *   - they've opted in via setEnabled(true),
 *   - the page is currently hidden (otherwise they're already watching it).
 */
export function fireResponseReady(text: string, session?: string): void {
  if (!isEnabled()) return;
  if (typeof document === "undefined") return;
  if (!document.hidden) return;
  const preview = (text || "").trim().slice(0, 140);
  const title = session ? `claude · ${session}` : "claude responded";
  try {
    const n = new Notification(title, {
      body: preview || "(response ready)",
      tag: "ccpipe-response",         // collapses duplicates
      silent: false,
    });
    n.onclick = () => {
      try { window.focus(); n.close(); } catch {}
    };
  } catch (e) {
    console.warn("notification failed:", e);
  }
}
