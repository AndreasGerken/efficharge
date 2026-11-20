// efficharge service worker: makes the app installable and survives a flaky connection.
// Everything comes from the network first, so pages, data, CSS and JS are never stale;
// the cache is only the offline fallback (static files, and /offline for pages).
"use strict";
const VERSION = "__VERSION__";  // replaced by the server: changes whenever a static file changes
const CACHE = `efficharge-${VERSION}`;
const PRECACHE = [
  "/offline",
  "/static/app.css",
  "/static/app.js",
  "/static/charts.js",
  "/static/vendor/htmx.min.js",
  "/static/vendor/chart.umd.min.js",
  "/static/icons/icon-192.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(PRECACHE)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    for (const key of await caches.keys()) if (key !== CACHE) await caches.delete(key);
    await self.clients.claim();
  })());
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== self.location.origin) return;

  if (req.mode === "navigate") {
    event.respondWith(fetch(req).catch(async () => (await caches.match("/offline")) || Response.error()));
    return;
  }
  if (url.pathname.startsWith("/static/")) {
    event.respondWith((async () => {
      try {
        const res = await fetch(req);
        if (res.ok) (await caches.open(CACHE)).put(req, res.clone());
        return res;
      } catch (e) {  // offline: any cached version of the file (ignore ?v=)
        return (await caches.match(req, { ignoreSearch: true })) || Response.error();
      }
    })());
  }
  // everything else (status fragments, chart JSON, API): network only
});
