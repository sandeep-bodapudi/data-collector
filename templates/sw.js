/* OneBridge Data Collector - service worker (build {{ build }}).
 *
 * What it does:
 *  - keeps the app's own files (code, styles, logo, icons) so the app opens instantly and works offline
 *  - keeps the last good copy of the app page so the installed app can start without a connection
 *  - never caches API calls or the login page: data always comes live from the server
 * Sheets are not handled here: they live in IndexedDB inside the browser.
 */
const BUILD = {{ build | tojson }};
const ASSETS = {{ assets | tojson }};
const SHELL = "shell-" + BUILD;   // versioned app files
const PAGES = "pages";            // last good copy of the app page, for starting offline
const FONTS = "fonts";

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(SHELL)
      .then((cache) => cache.addAll(ASSETS.map((url) => new Request(url, { cache: "reload" }))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    for (const name of await caches.keys()) {
      if (name !== SHELL && name !== FONTS) await caches.delete(name);  // old builds and the old page copy
    }
    try {  // fetch a fresh page copy that matches this build, so offline start works straight away
      const res = await fetch("/", { redirect: "manual", credentials: "same-origin" });
      if (res.ok) await (await caches.open(PAGES)).put("/", res);
    } catch (e) { /* offline or signed out: the next normal visit will store it */ }
    await self.clients.claim();
  })());
});

self.addEventListener("message", (event) => {
  if (event.data === "purge-pages") event.waitUntil(caches.delete(PAGES));  // on sign-out: forget the signed-in page
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);

  if (url.origin === self.location.origin) {
    const path = url.pathname;
    if (path.startsWith("/api/") || path === "/login" || path === "/logout" || path === "/sw.js") return;  // always live
    if (req.mode === "navigate") return event.respondWith(navigate(req, path));
    if (path.startsWith("/static/") || path === "/manifest.webmanifest" || path === "/offline.html") {
      return event.respondWith(cacheFirst(req));
    }
    return;
  }
  if (url.hostname === "fonts.googleapis.com" || url.hostname === "fonts.gstatic.com") {
    event.respondWith(staleWhileRevalidate(req, FONTS));
  }
});

async function navigate(req, path) {
  try {
    const res = await fetch(req);
    if (path === "/" && res.ok && !res.redirected && res.type !== "opaqueredirect") {
      const cache = await caches.open(PAGES);
      await cache.put("/", res.clone());
    }
    return res;
  } catch (e) {  // offline
    if (path === "/") {
      const page = await caches.match("/", { cacheName: PAGES });
      if (page) return page;
    }
    return (await caches.match("/offline.html")) || Response.error();
  }
}

async function cacheFirst(req) {
  const hit = await caches.match(req);
  if (hit) return hit;
  const res = await fetch(req);
  if (res.ok) (await caches.open(SHELL)).put(req, res.clone());
  return res;
}

async function staleWhileRevalidate(req, cacheName) {
  const cache = await caches.open(cacheName);
  const hit = await cache.match(req);
  const refresh = fetch(req).then((res) => { if (res.ok || res.type === "opaque") cache.put(req, res.clone()); return res; }).catch(() => hit);
  return hit || refresh;
}
