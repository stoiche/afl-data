// Offline support for the head-to-head app.
//
// Two different strategies on purpose:
//   - the page itself and its icons rarely change, so serve from cache first
//   - the results file changes twice a day, so always try the network first
//     and only fall back to the cached copy when there's no signal
const CACHE = "afl-h2h-v1";
const SHELL = ["./", "./index.html", "./manifest.json",
               "./icon-192.png", "./icon-512.png", "./icon-512-maskable.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  if (e.request.method !== "GET") return;
  const isData = e.request.url.includes("h2h_compact.json");

  if (isData) {
    e.respondWith(
      fetch(e.request)
        .then((res) => {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(e.request, copy));
          return res;
        })
        .catch(() => caches.match(e.request))   // offline: last results we saw
    );
    return;
  }

  e.respondWith(caches.match(e.request).then((hit) => hit || fetch(e.request)));
});
