/* Homestead's service worker: the installed app's shell, and its notifications.

   Pushes arrive empty. Homestead signs them, but cannot encrypt a payload
   without a crypto library, so a push only says "look": this worker then asks
   Homestead what happened, over the same signed-in connection the app uses,
   and shows that. If it cannot ask - signed out, or Cloudflare Access wants a
   fresh sign-in - it still says something needs attention.

   Caching is deliberately narrow. Versioned files (?v=) and vendored libraries
   never change under one address, so they are served from the cache. A page
   load always tries the network first and falls back to the saved shell only
   when there is no network at all. Nothing under /api/ is ever cached. */
const VERSION = "2.8.92";
const CACHE = `homestead-${VERSION}`;
const SHELL = "/";
const ICON = "/icons/icon-192.png";
const BADGE = "/icons/badge-96.png";

self.addEventListener("install", event => {
  event.waitUntil(caches.open(CACHE)
    .then(cache => cache.add(new Request(SHELL, { cache: "reload" })).catch(() => {}))
    .then(() => self.skipWaiting()));
});

self.addEventListener("activate", event => {
  event.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(key => key.startsWith("homestead-") && key !== CACHE)
      .map(key => caches.delete(key))))
    .then(() => self.clients.claim()));
});

const immutable = url => url.origin === self.location.origin &&
  (url.searchParams.has("v") || url.pathname.startsWith("/vendor/") || url.pathname.startsWith("/icons/"));

self.addEventListener("fetch", event => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin || url.pathname.startsWith("/api/") ||
      url.pathname.startsWith("/boot/")) return;
  if (request.mode === "navigate") {
    event.respondWith(fetch(request).then(response => {
      if (response.ok && response.type === "basic") {
        const copy = response.clone();
        caches.open(CACHE).then(cache => cache.put(SHELL, copy)).catch(() => {});
      }
      return response;
    }).catch(() => caches.match(SHELL).then(hit => hit || Response.error())));
    return;
  }
  if (immutable(url)) {
    event.respondWith(caches.match(request).then(hit => hit || fetch(request).then(response => {
      if (response.ok && response.type === "basic") {
        const copy = response.clone();
        caches.open(CACHE).then(cache => cache.put(request, copy)).catch(() => {});
      }
      return response;
    })));
  }
});

/* ------------------------------------------------------------ notifications */
async function pending() {
  const subscription = await self.registration.pushManager.getSubscription();
  const response = await fetch("/api/alerts/pending", {
    method: "POST", credentials: "same-origin", redirect: "error",
    headers: { "Content-Type": "application/json", "X-Homestead-Auth": "1" },
    body: JSON.stringify({ endpoint: subscription ? subscription.endpoint : "" }),
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

function badge(count) {
  const nav = self.navigator;
  if (!nav || !nav.setAppBadge) return Promise.resolve();
  return (count ? nav.setAppBadge(count) : nav.clearAppBadge()).catch(() => {});
}

async function announce() {
  let answer;
  try {
    answer = await pending();
  } catch (error) {
    return self.registration.showNotification("Homestead needs a look", {
      body: "Something changed. Open Homestead to see what - you may need to sign in again.",
      tag: "homestead-signin", icon: ICON, badge: BADGE, data: { href: "/" },
    });
  }
  await badge(answer.active || 0);
  const alerts = answer.alerts || [];
  if (!alerts.length) return;
  if (alerts.length > 3) {
    const critical = alerts.some(a => a.severity === "critical" && a.phase === "raised");
    return self.registration.showNotification(`${alerts.length} updates from Homestead`, {
      body: alerts.slice(-5).map(a => a.title).join("\n"),
      tag: "homestead-summary", renotify: true, icon: ICON, badge: BADGE,
      requireInteraction: critical, data: { href: "/" },
    });
  }
  await Promise.all(alerts.map(alert => self.registration.showNotification(alert.title, {
    body: alert.body || "",
    // One notification per problem: its resolution replaces it.
    tag: alert.key, renotify: true, icon: ICON, badge: BADGE,
    timestamp: (alert.at || 0) * 1000,
    requireInteraction: alert.severity === "critical" && alert.phase === "raised",
    data: { href: alert.href || "/" },
  })));
}

self.addEventListener("push", event => event.waitUntil(announce()));

self.addEventListener("notificationclick", event => {
  event.notification.close();
  const href = new URL((event.notification.data && event.notification.data.href) || "/", self.location.origin);
  if (href.origin !== self.location.origin) return;
  event.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(windows => {
    const open = windows.find(w => new URL(w.url).origin === self.location.origin);
    if (open) {
      open.postMessage({ type: "homestead-open", href: href.pathname + href.search });
      return open.focus();
    }
    return self.clients.openWindow(href.pathname + href.search);
  }));
});

function keyBytes(text) {
  const raw = atob(text.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - text.length % 4) % 4));
  return Uint8Array.from(raw, c => c.charCodeAt(0));
}

/* A browser may replace a subscription on its own; tell Homestead the new one. */
self.addEventListener("pushsubscriptionchange", event => {
  const old = event.oldSubscription;
  event.waitUntil((async () => {
    const key = await fetch("/api/push/key", { credentials: "same-origin" }).then(r => r.json());
    const fresh = event.newSubscription || await self.registration.pushManager.subscribe({
      userVisibleOnly: true, applicationServerKey: keyBytes(key.key) });
    await fetch("/api/push/subscribe", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-Homestead-Auth": "1" },
      body: JSON.stringify({ subscription: fresh.toJSON(), replaces: old ? old.endpoint : "" }),
    });
  })().catch(() => {}));
});
