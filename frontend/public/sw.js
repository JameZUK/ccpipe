// Minimal no-op service worker so the app is installable as a PWA.
// Real caching strategy can be added later; ccpipe is online-only by design.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});
