"use strict";

const NOMBRE_CACHE = "amadeus-movil-v1";
const RECURSOS = [
  "/",
  "/manifest.json",
  "/movil/style.css",
  "/movil/app.js"
];

self.addEventListener("install", (ev) => {
  ev.waitUntil(
    caches.open(NOMBRE_CACHE).then((cache) => cache.addAll(RECURSOS)).catch(() => {})
  );
  self.skipWaiting();
});

self.addEventListener("activate", (ev) => {
  ev.waitUntil(
    caches.keys().then((claves) =>
      Promise.all(claves.filter((k) => k !== NOMBRE_CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (ev) => {
  if (ev.request.method !== "GET") { return; }
  const url = new URL(ev.request.url);
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/rams/") || url.pathname === "/visor3d") {
    return;
  }
  ev.respondWith(
    fetch(ev.request).catch(() => caches.match(ev.request))
  );
});
