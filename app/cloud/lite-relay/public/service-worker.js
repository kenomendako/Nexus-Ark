const CACHE_NAME = "nexus-ark-lite-travel-phase5-v51";
const CACHE_NAMESPACE = CACHE_NAME.replace(/\d+$/, "");
const NETWORK_TIMEOUT_MS = 2500;
const ASSETS = [
  "/",
  "/manifest.webmanifest",
  "/static/styles.css?v=116",
  "/static/app.js?v=116",
  "/static/pairing-handoff.js?v=116",
  "/static/lite-continuity-state.js?v=116",
  "/static/travel-adapter.js?v=116",
  "/static/lite-update-state.js?v=116",
  "/icon.png",
  "/icon-maskable.png",
  "/badge.png"
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(ASSETS)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((key) => key.startsWith(CACHE_NAMESPACE) && key !== CACHE_NAME)
          .map((key) => caches.delete(key)),
      )
    ).then(() => self.clients.claim())
  );
});

function isNavigationRequest(request) {
  return request.mode === "navigate"
    || request.destination === "document"
    || isKnownNavigation(new URL(request.url));
}

function isKnownNavigation(url) {
  return url.pathname === "/" || url.pathname === "/";
}

function isExcludedRequest(request, url) {
  if (request.method !== "GET" || url.origin !== self.location.origin) {
    return true;
  }
  if (url.pathname === "/api" || url.pathname.startsWith("/api/")) {
    return true;
  }
  if (url.pathname === "/v1" || url.pathname.startsWith("/v1/")) {
    return true;
  }
  if (url.pathname === "/health" || url.pathname.startsWith("/health/")) {
    return true;
  }
  if (url.pathname.endsWith("/lite-build.json")) {
    return true;
  }
  const headers = request.headers;
  return [
    "authorization",
    "cookie",
    "proxy-authorization",
    "x-api-key",
    "x-auth-token",
    "x-owner-token",
  ].some((name) => headers?.has?.(name));
}

function knownAssetUrls() {
  return new Set(ASSETS.map((asset) => new URL(asset, self.location.origin).href));
}

function isKnownShellRequest(request, url) {
  if (isNavigationRequest(request)) {
    return isKnownNavigation(url);
  }
  return knownAssetUrls().has(url.href);
}

function canonicalNavigationCacheKey() {
  return new URL("/", self.location.origin).href;
}

function isHtmlResponse(response) {
  const contentType = response.headers?.get?.("content-type") || "";
  return contentType.toLowerCase().includes("text/html");
}

function offlineResponse(navigation) {
  if (navigation) {
    return new Response(
      "<!doctype html><meta charset=\"utf-8\"><title>Nexus Ark Lite オフライン</title>"
        + "<h1>Nexus Ark Liteを開けません</h1>"
        + "<p>通信を確認できず、保存済みの画面も利用できません。</p>",
      {
        status: 503,
        headers: {
          "cache-control": "no-store",
          "content-type": "text/html; charset=utf-8",
        },
      },
    );
  }
  return new Response("Nexus Ark Lite is offline.", {
    status: 503,
    headers: {
      "cache-control": "no-store",
      "content-type": "text/plain; charset=utf-8",
    },
  });
}

async function fetchWithTimeout(request) {
  const controller = typeof AbortController === "function" ? new AbortController() : null;
  let timeoutId;
  const timeout = new Promise((_, reject) => {
    timeoutId = setTimeout(() => {
      controller?.abort();
      reject(new Error("Lite shell fetch timed out."));
    }, NETWORK_TIMEOUT_MS);
  });
  try {
    return await Promise.race([
      fetch(request, { cache: "no-store", signal: controller?.signal }).then(async (response) => {
        if (!response.ok) return response;
        // headersだけ到着して本文が止まる場合も、画面取得の期限に含める。
        const body = await response.arrayBuffer();
        return new Response(body, {
          status: response.status,
          statusText: response.statusText,
          headers: response.headers,
        });
      }),
      timeout,
    ]);
  } finally {
    clearTimeout(timeoutId);
  }
}

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (isExcludedRequest(event.request, url) || !isKnownShellRequest(event.request, url)) {
    return;
  }
  const navigation = isNavigationRequest(event.request);
  const cacheKey = navigation ? canonicalNavigationCacheKey() : event.request;
  const shellRequest = navigation ? new Request(cacheKey, { credentials: "same-origin" }) : event.request;
  const fetchAndCache = (cache) => fetchWithTimeout(shellRequest)
    .then((response) => {
      if (navigation && (!response.ok || !isHtmlResponse(response))) {
        return offlineResponse(true);
      }
      if (response.status >= 500) {
        return offlineResponse(navigation);
      }
      if (response.ok && cache) {
        const copy = response.clone();
        event.waitUntil(cache.put(cacheKey, copy).catch(() => {}));
      }
      return response;
    })
    .catch(() => offlineResponse(navigation));
  event.respondWith(
    caches.open(CACHE_NAME)
      .then((cache) => cache.match(cacheKey).then((cached) => cached || fetchAndCache(cache)))
      .catch(() => fetchAndCache(null))
  );
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
    icon: "/icon.png",
    badge: "/badge.png",
    tag: payload.tag || "nexus-ark-lite-web-push",
    data: {
      url: payload.url || new URL("/", self.location.origin).href
    }
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const targetUrl = event.notification.data?.url || new URL("/", self.location.origin).href;
  event.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then((clientList) => {
      for (const client of clientList) {
        const url = new URL(client.url);
        if (url.pathname.startsWith("/")) {
          return client.focus();
        }
      }
      return clients.openWindow(new URL(targetUrl, self.location.origin).href);
    })
  );
});
