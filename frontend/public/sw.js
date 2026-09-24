// Self-retiring service worker. ccpipe no longer registers a service
// worker (see main.ts), but an old install may still have this URL
// registered. The browser periodically re-fetches the script; this
// version takes over immediately and unregisters itself, so any leftover
// worker is retired. Nothing ever registers this file — don't add that.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => {
  e.waitUntil(
    self.clients.claim().then(() => self.registration.unregister()),
  );
});
