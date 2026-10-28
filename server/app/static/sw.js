// Keeps the app shell so the page opens even with no connection. Measuring itself needs the server, so the API is never cached.
const SHELL = "photo-shell-v1";
const FILES = ["/photo.html", "/photo.webmanifest", "/icons/photo-192.png", "/icons/photo-512.png"];

self.addEventListener("install", e => { e.waitUntil(caches.open(SHELL).then(c => c.addAll(FILES)).then(() => self.skipWaiting())); });
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== SHELL).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", e => {
  const u = new URL(e.request.url);
  if (e.request.method !== "GET" || u.origin !== location.origin || u.pathname.startsWith("/api/") || !FILES.includes(u.pathname)) return;
  // network first so a new deploy shows up at once; the saved copy is only for when the server cannot be reached
  e.respondWith(fetch(e.request).then(r => { if (r.ok) { const copy = r.clone(); caches.open(SHELL).then(c => c.put(e.request, copy)); } return r; })   // an error page must not replace the saved copy
    .catch(() => caches.match(e.request)));
});
