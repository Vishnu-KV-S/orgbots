/*
 * The service worker: push notifications, and nothing else.
 *
 * It caches nothing — the app is live data from the runtime, and a cached page would be
 * a stale one. It shows the notification the runtime pushed (a bot replied, asked, or
 * needs an approval or a sign-in) and, on a click, focuses an open window of the app or
 * opens one, at the conversation the notification is about.
 */

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch {
    data = { title: "Bots", body: event.data ? event.data.text() : "" };
  }
  event.waitUntil(
    self.registration.showNotification(data.title || "Bots", {
      body: data.body || "",
      tag: data.tag || "bots",
      renotify: true,
      icon: "/icon-192x192.png",
      badge: "/icon-192x192.png",
      data: { url: data.url || "/" },
    }),
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = new URL((event.notification.data && event.notification.data.url) || "/", self.location.origin);
  event.waitUntil(
    (async () => {
      const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
      for (const client of windows) {
        if (new URL(client.url).origin === url.origin) {
          await client.focus();
          if ("navigate" in client) await client.navigate(url.href);
          return;
        }
      }
      await self.clients.openWindow(url.href);
    })(),
  );
});
