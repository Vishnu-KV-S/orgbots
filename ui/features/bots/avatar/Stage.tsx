"use client";

import { PerspectiveCamera, View } from "@react-three/drei";
import { Canvas, useThree } from "@react-three/fiber";
import { createContext, type CSSProperties, useContext, useEffect, useMemo, useState } from "react";
import * as THREE from "three";
import { cx } from "@/lib/cx";
import type { Appearance } from "./appearance";
import { BotModel } from "./BotModel";
import type { Mood } from "./mood";

/**
 * Every 3D bot on screen is drawn by **one** WebGL canvas.
 *
 * Browsers cap live WebGL contexts at around sixteen, and a sidebar of bots would
 * blow through that with a canvas each. So the canvas is fixed behind the page and
 * each `<BotFace>` is a drei `View`: a DOM box the canvas scissors into and renders
 * that bot's scene into. One context, any number of bots, and each still lays out
 * like an ordinary element.
 *
 * Reflections come from a studio environment generated on the GPU from
 * a product-photography studio built in code — softboxes on a dark cyclorama, no
 * HDR file to download — once per renderer, shared by every bot's scene.
 */

const StageContext = createContext<{ ready: boolean }>({ ready: false });

function webglAvailable(): boolean {
  try {
    const canvas = document.createElement("canvas");
    return Boolean(canvas.getContext("webgl2") ?? canvas.getContext("webgl"));
  } catch {
    return false;
  }
}

let sharedEnvironment: {
  gl: THREE.WebGLRenderer;
  texture: THREE.Texture;
} | null = null;

/**
 * What polished surfaces reflect: a dim studio lit by a few large softboxes.
 *
 * A generic room environment puts furniture-shaped highlights into every glossy
 * surface, and behind a dark visor they read as things glowing *inside* it. A real
 * product shot reflects only clean light shapes — a big overhead box, two tall side
 * strips, a rim behind — and this is exactly that, rendered once into a cube map.
 *
 * The room is darker than the boxes by a wide margin on purpose: a glossy white shell
 * only looks glossy where its reflections differ from its own colour, so the clear
 * coat needs bright windows to mirror and a dim room between them to give the curves
 * their edge. Nothing bright sits low in front, because the lower half of a visor
 * mirrors it, and a bright bar under the eyes reads as a mouth.
 */
function softboxStudio(): THREE.Scene {
  const scene = new THREE.Scene();
  // Mid-grey walls: polished steel reflects its surroundings, and in a black room
  // it reads as black chrome.
  scene.background = new THREE.Color("#3b3d43");
  const box = (w: number, h: number, intensity: number, pos: THREE.Vector3Tuple) => {
    const mesh = new THREE.Mesh(
      new THREE.PlaneGeometry(w, h),
      new THREE.MeshBasicMaterial({
        color: new THREE.Color(1, 1, 1).multiplyScalar(intensity),
        side: THREE.DoubleSide,
      }),
    );
    mesh.position.set(...pos);
    mesh.lookAt(0, 0, 0);
    scene.add(mesh);
  };
  box(7, 3.5, 6, [0, 6, 1.5]); // overhead key
  box(6, 2.5, 5, [0, 4.5, -3.5]); // a strip high behind: a crisp line along every top edge
  box(2, 6, 4.2, [-5.5, 1.5, 2.5]); // left strip
  box(2, 6, 3.2, [5.5, 1.5, 1.5]); // right strip
  box(4, 5, 2.4, [0, 1.5, -6]); // rim behind
  box(3, 1.4, 1.6, [-3, 3, 5]); // a small window front-left: the visor's catchlight
  // A faint floor bounce, so undersides are not pure black.
  const floor = new THREE.Mesh(
    new THREE.PlaneGeometry(30, 30),
    new THREE.MeshBasicMaterial({ color: "#2c2d31" }),
  );
  floor.rotation.x = -Math.PI / 2;
  floor.position.y = -3;
  scene.add(floor);
  return scene;
}

function studioEnvironment(gl: THREE.WebGLRenderer): THREE.Texture {
  if (sharedEnvironment?.gl === gl) return sharedEnvironment.texture;
  const pmrem = new THREE.PMREMGenerator(gl);
  const texture = pmrem.fromScene(softboxStudio(), 0.02).texture;
  pmrem.dispose();
  sharedEnvironment = { gl, texture };
  return texture;
}

/** Puts the shared environment on whichever scene this is rendered into. */
function Studio() {
  const gl = useThree((s) => s.gl);
  const scene = useThree((s) => s.scene);
  useEffect(() => {
    scene.environment = studioEnvironment(gl);
    scene.environmentIntensity = 1.25;
  }, [gl, scene]);
  return null;
}

export function BotStage({ children }: { children: React.ReactNode }) {
  const [ready, setReady] = useState(false);
  useEffect(() => setReady(webglAvailable()), []);
  const value = useMemo(() => ({ ready }), [ready]);

  return (
    <StageContext.Provider value={value}>
      {children}
      {ready && (
        <Canvas
          className="bot-stage"
          // Above ordinary content (whose backgrounds would otherwise cover it), below
          // every menu, dialog and toast. A face inside a dialog uses its own canvas —
          // see `standalone` — because a dialog sits above this one.
          style={{
            position: "fixed",
            inset: 0,
            pointerEvents: "none",
            zIndex: 1,
          }}
          dpr={[1, 2]}
          gl={{
            antialias: true,
            alpha: true,
            powerPreference: "high-performance",
          }}
          onCreated={({ gl }) => toneMap(gl)}
        >
          <View.Port />
        </Canvas>
      )}
    </StageContext.Provider>
  );
}

/** AgX: a filmic curve that rolls bright LEDs off toward white without shifting their
 * hue, the way a camera sensor does. Every canvas that draws a bot sets this. */
export function toneMap(gl: THREE.WebGLRenderer) {
  gl.toneMapping = THREE.AgXToneMapping;
  gl.toneMappingExposure = 1.25;
}

/**
 * One bot in its studio: camera, environment, lights and the model. Everything that
 * makes a bot look like itself, so every canvas that draws one — the web app's shared
 * stage, a standalone face, and the mobile app's embedded renderer (`mobile/bot3d`) —
 * draws exactly the same thing.
 */
export function BotScene({
  appearance,
  mood,
  detail,
  phase,
  framing,
}: {
  appearance: Appearance;
  mood: Mood;
  detail: "high" | "low";
  phase: number;
  framing: "full" | "head";
}) {
  const camera: [number, number, number] =
    framing === "head" ? [0, 0.1, 2.75] : detail === "high" ? [0, 0.22, 3.1] : [0, 0.1, 2.9];
  return (
    <>
      <PerspectiveCamera
        makeDefault
        position={camera}
        fov={30}
        onUpdate={(c) => c.lookAt(0, 0.04, 0)}
      />
      <Studio />
      {/* A neutral three-point studio rig: warm key, cool fill, white rim. */}
      <ambientLight intensity={0.32} />
      <directionalLight position={[2.2, 3.2, 2.8]} intensity={2.4} color="#ffffff" />
      <directionalLight position={[-2.8, 0.8, 1.5]} intensity={1.3} color="#ffffff" />
      <directionalLight position={[0, 2, -3]} intensity={1.4} color="#ffffff" />
      <BotModel appearance={appearance} mood={mood} detail={detail} phase={phase} />
    </>
  );
}

/** A flat CSS face in the bot's colours: the fallback without WebGL, and the
 * cheap version for places with many small faces (message avatars). */
export function FlatFace({
  appearance,
  size,
  className,
}: {
  appearance: Appearance;
  size: number;
  className?: string;
}) {
  const style = {
    width: size,
    height: size,
    "--body": appearance.body,
    "--glow": appearance.glow,
    borderRadius:
      appearance.shape === "orb" ? "50%" : appearance.shape === "capsule" ? "40%" : "24%",
  } as CSSProperties;
  return (
    <span className={cx("flatface", className)} style={style} aria-hidden>
      <span className="flatface-visor">
        <span className={cx("flatface-eye", `eye-${appearance.eyes}`)} />
        {appearance.eyes !== "visor" && (
          <span className={cx("flatface-eye", `eye-${appearance.eyes}`)} />
        )}
      </span>
    </span>
  );
}

/**
 * A live 3D bot. `size` is the box in CSS pixels; `detail="low"` drops the floor and
 * the particle effects, which read as noise at sidebar size.
 */
export function BotFace({
  appearance,
  mood,
  size,
  detail,
  className,
  seed = "",
  framing = "full",
  standalone = false,
}: {
  appearance: Appearance;
  mood: Mood;
  size: number;
  detail?: "high" | "low";
  className?: string;
  /** Anything stable per bot — its id — so a list does not bob in lockstep. */
  seed?: string;
  /** `head` crops in on the face, for small avatars. */
  framing?: "full" | "head";
  /** Draw into a canvas of its own. For faces inside dialogs, which sit above the
   * shared canvas; costs one WebGL context, so only for one or two faces at a time. */
  standalone?: boolean;
}) {
  const { ready } = useContext(StageContext);
  const level = detail ?? (size >= 96 ? "high" : "low");
  const phase = useMemo(() => {
    let h = 0;
    for (let i = 0; i < seed.length; i++) h = (h * 31 + seed.charCodeAt(i)) % 1000;
    return h / 100;
  }, [seed]);

  if (!ready) {
    return <FlatFace appearance={appearance} size={size} className={className} />;
  }
  const scene = (
    <BotScene appearance={appearance} mood={mood} detail={level} phase={phase} framing={framing} />
  );
  if (standalone) {
    return (
      <span className={cx("botface", className)} style={{ width: size, height: size }}>
        <Canvas dpr={[1, 2]} gl={{ antialias: true, alpha: true }} onCreated={({ gl }) => toneMap(gl)}>
          {scene}
        </Canvas>
      </span>
    );
  }
  return (
    <View className={cx("botface", className)} style={{ width: size, height: size }}>
      {scene}
    </View>
  );
}
