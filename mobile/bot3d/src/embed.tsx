/**
 * The web app's 3D bots, for the phone.
 *
 * This is not a copy: it imports `BotScene` — camera, studio lighting and `BotModel` —
 * straight from `ui/features/bots/avatar`, and `build.mjs` bundles it into one HTML
 * file the Flutter app ships as an asset and shows in a web view. A bot on the phone is
 * the same code, the same shapes, eyes, moods and materials as in the browser.
 *
 * The page is driven from Dart through `window.orgbots`:
 *
 *   show({ appearance, mood, framing, detail, seed })   the live, animated bot
 *   snapshot({ id, appearance, mood, framing, size })   render once, reply with a PNG
 *
 * (or, inside an iframe, as `{ call, arg }` messages from the parent page), and answers
 * on the `Bot3D` channel (`window.Bot3D.postMessage`, which webview_flutter
 * injects) or, inside an iframe, `parent.postMessage`:
 *
 *   { type: "ready", webgl }   { type: "snapshot", id, data }   { type: "error", message }
 */

import { Canvas } from "@react-three/fiber";
import { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import type * as THREE from "three";
import type { Appearance } from "../../../ui/features/bots/avatar/appearance";
import { appearanceFor } from "../../../ui/features/bots/avatar/appearance";
import type { Mood } from "../../../ui/features/bots/avatar/mood";
import { BotScene, toneMap } from "../../../ui/features/bots/avatar/Stage";

interface Show {
  appearance?: Partial<Appearance>;
  mood?: Mood;
  framing?: "full" | "head";
  detail?: "high" | "low";
  seed?: string;
}

interface Snapshot extends Show {
  id: string;
  /** Pixels on a side. */
  size?: number;
}

type Message =
  | { type: "ready"; webgl: boolean }
  | { type: "snapshot"; id: string; data: string }
  | { type: "error"; message: string; id?: string };

function post(message: Message) {
  const text = JSON.stringify(message);
  const channel = (window as unknown as { Bot3D?: { postMessage: (m: string) => void } }).Bot3D;
  if (channel) channel.postMessage(text);
  else if (window.parent !== window) window.parent.postMessage(text, "*");
}

const look = (show: Show): Appearance =>
  appearanceFor({ id: show.seed ?? "bot", appearance: show.appearance ?? {} });

const phaseOf = (seed = "") => {
  let h = 0;
  for (let i = 0; i < seed.length; i++) h = (h * 31 + seed.charCodeAt(i)) % 1000;
  return h / 100;
};

function webglAvailable(): boolean {
  try {
    const canvas = document.createElement("canvas");
    return Boolean(canvas.getContext("webgl2") ?? canvas.getContext("webgl"));
  } catch {
    return false;
  }
}

// --- state the Dart side sets -------------------------------------------------------

let setLive: (show: Show) => void = () => undefined;
const queue: Snapshot[] = [];
/** Set once the snapshot canvas is mounted; until then requests just queue. */
let setShot: ((shot: Snapshot | null) => void) | null = null;
let shooting = false;

function nextShot() {
  if (shooting || !setShot) return;
  const shot = queue.shift();
  if (!shot) return setShot(null);
  shooting = true;
  setShot(shot);
}

const api = {
  show: (show: Show) => setLive(show),
  snapshot: (shot: Snapshot) => {
    queue.push(shot);
    nextShot();
  },
};
(window as unknown as { orgbots: typeof api }).orgbots = api;

// Inside an iframe (the app's web build) the same calls arrive as messages.
window.addEventListener("message", (event) => {
  if (event.source !== window.parent || typeof event.data !== "string") return;
  try {
    const { call, arg } = JSON.parse(event.data) as { call: keyof typeof api; arg: never };
    if (call in api) api[call](arg);
  } catch {
    /* not ours */
  }
});

// --- the live bot ---------------------------------------------------------------------

function Live() {
  const [show, setShow] = useState<Show | null>(() => {
    // `#<json>` lets a page be opened already showing a bot, with no round trip.
    try {
      return location.hash.length > 1 ? (JSON.parse(decodeURIComponent(location.hash.slice(1))) as Show) : null;
    } catch {
      return null;
    }
  });
  useEffect(() => {
    setLive = setShow;
  }, []);
  if (!show) return null;
  return (
    <Canvas
      style={{ position: "fixed", inset: 0 }}
      dpr={[1, 2]}
      gl={{ antialias: true, alpha: true, powerPreference: "high-performance" }}
      onCreated={({ gl }) => toneMap(gl)}
    >
      <BotScene
        appearance={look(show)}
        mood={show.mood ?? "idle"}
        detail={show.detail ?? "high"}
        phase={phaseOf(show.seed)}
        framing={show.framing ?? "full"}
      />
    </Canvas>
  );
}

// --- snapshots ------------------------------------------------------------------------

/** Frames to let the environment map, materials and the mood's pose settle. */
const SETTLE_FRAMES = 36;

function Shooter() {
  const [shot, setShotState] = useState<Snapshot | null>(null);
  useEffect(() => {
    setShot = setShotState;
    // Only now can a request be served: say so, then serve any that came early.
    post({ type: "ready", webgl: true });
    nextShot();
  }, []);
  if (!shot) return null;
  const size = shot.size ?? 192;
  return (
    <div style={{ position: "fixed", left: 0, top: 0, width: size, height: size, opacity: 0, pointerEvents: "none" }}>
      <Canvas
        key={shot.id}
        dpr={1}
        gl={{ antialias: true, alpha: true, preserveDrawingBuffer: true }}
        onCreated={({ gl }) => {
          toneMap(gl);
          capture(gl, shot);
        }}
      >
        <BotScene
          appearance={look(shot)}
          mood={shot.mood ?? "idle"}
          detail="low"
          phase={0}
          framing={shot.framing ?? "head"}
        />
      </Canvas>
    </div>
  );
}

function capture(gl: THREE.WebGLRenderer, shot: Snapshot) {
  let frames = 0;
  const tick = () => {
    if (++frames < SETTLE_FRAMES) return void requestAnimationFrame(tick);
    try {
      post({ type: "snapshot", id: shot.id, data: gl.domElement.toDataURL("image/png") });
    } catch (cause) {
      post({ type: "error", id: shot.id, message: String(cause) });
    }
    shooting = false;
    nextShot();
  };
  requestAnimationFrame(tick);
}

// --- boot -----------------------------------------------------------------------------

// With WebGL, `ready` is sent by <Shooter> once it is mounted; without, right away,
// so the app falls back to flat faces.
if (webglAvailable()) {
  createRoot(document.getElementById("root")!).render(
    <>
      <Live />
      <Shooter />
    </>,
  );
} else {
  post({ type: "ready", webgl: false });
}
window.addEventListener("error", (e) => post({ type: "error", message: String(e.message) }));
