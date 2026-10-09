import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import {
  type Me,
  authMe,
  normalizeServer,
  setServer,
  setUnauthorizedHandler,
  signOut as apiSignOut,
} from "./api";
import { getItem, removeItem, setItem } from "./storage";

/**
 * Which server this phone uses, and who is signed in to it.
 *
 * `status` drives the first screen: no server yet → connect; a server with members
 * and no session → sign in; otherwise the bots. Only the server address is stored
 * here — the session itself is the web app's HttpOnly cookie, kept by the OS.
 */

const SERVER_KEY = "orgbots.server";

export type Status = "loading" | "no-server" | "signed-out" | "ready" | "offline";

interface Session {
  status: Status;
  server: string;
  mode: "none" | "members";
  me: Me | null;
  error: string | null;
  /** Probe a server and, if it answers as Orgbots, make it this phone's. */
  connect: (input: string) => Promise<void>;
  /** Ask the server again who we are: after signing in, or to retry when offline. */
  refresh: () => Promise<Status>;
  signOut: () => Promise<void>;
  /** Forget the server altogether. */
  disconnect: () => Promise<void>;
}

const SessionContext = createContext<Session | null>(null);

export function useSession(): Session {
  const session = useContext(SessionContext);
  if (!session) throw new Error("useSession outside SessionProvider");
  return session;
}

export function SessionProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<Status>("loading");
  const [server, setServerState] = useState("");
  const [mode, setMode] = useState<"none" | "members">("none");
  const [me, setMe] = useState<Me | null>(null);
  const [error, setError] = useState<string | null>(null);

  const probe = useCallback(async (): Promise<Status> => {
    let next: Status;
    try {
      const who = await authMe();
      setMode(who.mode);
      setMe(who.member);
      setError(null);
      next = who.mode === "members" && !who.member ? "signed-out" : "ready";
    } catch (cause) {
      setError((cause as Error).message);
      next = "offline";
    }
    setStatus(next);
    return next;
  }, []);

  useEffect(() => {
    void (async () => {
      const saved = await getItem(SERVER_KEY).catch(() => null);
      if (!saved) {
        setStatus("no-server");
        return;
      }
      setServer(saved);
      setServerState(saved);
      await probe();
    })();
  }, [probe]);

  useEffect(() => {
    setUnauthorizedHandler(() => {
      setMe(null);
      setStatus("signed-out");
    });
    return () => setUnauthorizedHandler(null);
  }, []);

  const connect = useCallback(async (input: string) => {
    const url = normalizeServer(input);
    if (!url) throw new Error("That doesn't look like a server address");
    setServer(url);
    let who: Awaited<ReturnType<typeof authMe>>;
    try {
      who = await authMe();
    } catch (cause) {
      throw new Error(
        `${(cause as Error).message}. Check the address and that the Orgbots web app is running there.`,
      );
    }
    if (!who || (who.mode !== "none" && who.mode !== "members")) {
      throw new Error(`${url} answered, but not as an Orgbots server`);
    }
    await setItem(SERVER_KEY, url);
    setServerState(url);
    setMode(who.mode);
    setMe(who.member);
    setError(null);
    setStatus(who.mode === "members" && !who.member ? "signed-out" : "ready");
  }, []);

  const signOut = useCallback(async () => {
    await apiSignOut().catch(() => undefined);
    setMe(null);
    setStatus(mode === "members" ? "signed-out" : "ready");
  }, [mode]);

  const disconnect = useCallback(async () => {
    if (mode === "members") await apiSignOut().catch(() => undefined);
    await removeItem(SERVER_KEY).catch(() => undefined);
    setServer("");
    setServerState("");
    setMe(null);
    setStatus("no-server");
  }, [mode]);

  const value = useMemo(
    () => ({ status, server, mode, me, error, connect, refresh: probe, signOut, disconnect }),
    [status, server, mode, me, error, connect, probe, signOut, disconnect],
  );
  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}
