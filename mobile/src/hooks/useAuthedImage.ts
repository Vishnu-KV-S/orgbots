import { useEffect, useState } from "react";

const cache = new Map<string, string>();

/**
 * An image the server only gives to a signed-in session, as a data URI.
 *
 * The image views' own loaders do not reliably carry the session cookie on Android,
 * so the bytes come through `fetch` — the stack that does — and are handed over as a
 * data URI. Screenshots never change, so each is fetched once per app run.
 */
export function useAuthedImage(url: string | null): { uri: string | null; gone: boolean } {
  const [loaded, setLoaded] = useState<{ url: string; uri: string | null } | null>(null);

  useEffect(() => {
    if (!url || cache.has(url)) return;
    let live = true;
    void (async () => {
      let uri: string | null = null;
      try {
        const response = await fetch(url, { credentials: "include" });
        if (!response.ok) throw new Error(String(response.status));
        const blob = await response.blob();
        uri = await new Promise<string>((resolve, reject) => {
          const reader = new FileReader();
          reader.onload = () => resolve(String(reader.result));
          reader.onerror = () => reject(reader.error);
          reader.readAsDataURL(blob);
        });
        cache.set(url, uri);
      } catch {
        uri = null;
      }
      if (live) setLoaded({ url, uri });
    })();
    return () => {
      live = false;
    };
  }, [url]);

  if (!url) return { uri: null, gone: false };
  const hit = cache.get(url);
  if (hit) return { uri: hit, gone: false };
  const done = loaded?.url === url;
  return { uri: done ? loaded.uri : null, gone: done && !loaded.uri };
}
