import { pushKey, pushSubscribe, pushUnsubscribe } from "@/lib/api/bots";

/**
 * Push notifications to this device: the service worker (`public/sw.js`) and a Web Push
 * subscription the runtime's notifier sends to. They arrive with the app closed —
 * installed, or in any browser that keeps service workers alive.
 */

export type PushState = "unsupported" | "denied" | "on" | "off";

function supported(): boolean {
  return (
    typeof window !== "undefined" &&
    "serviceWorker" in navigator &&
    "PushManager" in window &&
    "Notification" in window
  );
}

export async function registerServiceWorker(): Promise<ServiceWorkerRegistration | null> {
  if (!supported()) return null;
  try {
    return await navigator.serviceWorker.register("/sw.js", {
      scope: "/",
      updateViaCache: "none",
    });
  } catch {
    return null;
  }
}

export async function pushState(): Promise<PushState> {
  if (!supported()) return "unsupported";
  if (Notification.permission === "denied") return "denied";
  const registration = await navigator.serviceWorker.getRegistration("/");
  const subscription = await registration?.pushManager.getSubscription();
  return subscription ? "on" : "off";
}

function keyBytes(base64url: string): Uint8Array<ArrayBuffer> {
  const padded = (base64url + "=".repeat((4 - (base64url.length % 4)) % 4))
    .replace(/-/g, "+")
    .replace(/_/g, "/");
  const raw = atob(padded);
  const out = new Uint8Array(new ArrayBuffer(raw.length));
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}

/** Ask permission, subscribe at the browser's push service, and tell the runtime. */
export async function enablePush(): Promise<PushState> {
  if (!supported()) return "unsupported";
  if ((await Notification.requestPermission()) !== "granted") return "denied";
  const registration = (await registerServiceWorker()) ?? (await navigator.serviceWorker.ready);
  const { public_key } = await pushKey();
  const subscription =
    (await registration.pushManager.getSubscription()) ??
    (await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: keyBytes(public_key),
    }));
  await pushSubscribe(subscription.toJSON(), navigator.userAgent);
  return "on";
}

export async function disablePush(): Promise<PushState> {
  if (!supported()) return "unsupported";
  const registration = await navigator.serviceWorker.getRegistration("/");
  const subscription = await registration?.pushManager.getSubscription();
  if (subscription) {
    await pushUnsubscribe(subscription.endpoint);
    await subscription.unsubscribe();
  }
  return "off";
}
