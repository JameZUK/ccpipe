// ccpipe's push notifications (push.ts registers this when you turn them on
// in Settings → Notifications). It only shows the notifications the server
// pushes and opens the session you tap. There is no fetch handler: it never
// stands between a page and the server, and caches nothing.
//
// The server sends {title, body, tag, session, level, count}, encrypted for
// this device (backend webpush.py).

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch { d = {}; }
  const show = self.registration.showNotification(d.title || "ccpipe", {
    body: d.body || "",
    tag: d.tag || "ccpipe",
    renotify: true,
    icon: "/icon-192.png",
    badge: "/icon-192.png",
    data: { session: d.session || "", level: d.level || "" },
  });
  const badge = typeof d.count === "number" && self.navigator.setAppBadge
    ? self.navigator.setAppBadge(d.count).catch(() => {})
    : Promise.resolve();
  e.waitUntil(Promise.all([show, badge]));
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const session = (e.notification.data && e.notification.data.session) || "";
  e.waitUntil((async () => {
    // The app's own page (not /history or a file viewer, which can't open
    // a session), the one in front if there are several.
    const wins = (await self.clients.matchAll({ type: "window", includeUncontrolled: true }))
      .filter((w) => {
        const u = new URL(w.url);
        return u.origin === self.location.origin && u.pathname === "/";
      })
      .sort((a, b) => Number(b.focused) - Number(a.focused)
        || Number(b.visibilityState === "visible") - Number(a.visibilityState === "visible"));
    const w = wins[0];
    if (w) {
      try { await w.focus(); } catch { /* not allowed: still tell it */ }
      w.postMessage({ type: "ccpipe-open", session });
      return;
    }
    await self.clients.openWindow(session ? `/?open=${encodeURIComponent(session)}` : "/");
  })());
});

// The browser replaced the subscription (Firefox does now and then): tell
// the server, which keeps the device's settings.
self.addEventListener("pushsubscriptionchange", (e) => {
  e.waitUntil((async () => {
    const old = e.oldSubscription;
    let sub = e.newSubscription;
    const key = old && old.options && old.options.applicationServerKey;
    if (!sub && key) {
      sub = await self.registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
    }
    if (!sub) return;
    const json = sub.toJSON();
    await fetch("/api/push/subscribe", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-Requested-By": "ccpipe" },
      body: JSON.stringify({ endpoint: json.endpoint, keys: json.keys, renew: old ? old.endpoint : "" }),
    });
  })());
});
