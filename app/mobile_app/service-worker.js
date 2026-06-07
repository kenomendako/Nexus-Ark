const CACHE_NAME = "nexus-ark-lite-v55";
const ASSETS = [
  "/lite",
  "/lite/",
  "/lite/manifest.webmanifest",
  "/lite/static/styles.css",
  "/lite/static/app.js",
  "/lite/icon.png",
  "/lite/badge.png"
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(ASSETS)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (url.pathname.startsWith("/api/")) {
    return;
  }
  event.respondWith(caches.match(event.request).then((cached) => cached || fetch(event.request)));
});

self.addEventListener("push", (event) => {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch {
    payload = { body: event.data?.text?.() || "" };
  }
  const title = payload.title || "Nexus Ark Lite";
  const options = {
    body: payload.body || "通知を受信しました。",
    icon: "/lite/icon.png",
    badge: "/lite/badge.png",
    tag: payload.tag || "nexus-ark-lite-web-push",
    data: {
      url: payload.url || new URL("/lite/", self.location.origin).href
    }
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const targetUrl = event.notification.data?.url || new URL("/lite/", self.location.origin).href;
  event.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then((clientList) => {
      for (const client of clientList) {
        const url = new URL(client.url);
        if (url.pathname.startsWith("/lite")) {
          return client.focus();
        }
      }
      return clients.openWindow(new URL(targetUrl, self.location.origin).href);
    })
  );
});
