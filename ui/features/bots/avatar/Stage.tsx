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
 * What polished surfaces reflect: a dark studio lit by a few large softboxes.
 *
 * A generic room environment puts furniture-shaped highlights into every glossy
 * surface, and behind a dark visor they read as things glowing *inside* it. A real
 * product shot reflects only clean light shapes — a big overhead box, two tall side
 * strips, a low fill card — and this is exactly that, rendered once into a cube map.
 */
function softboxStudio(): THREE.Scene {
  const scene = new THREE.Scene();
  // Mid-grey walls: polished steel reflects its surroundings, and in a black room
  // it reads as black chrome. Glass, which reflects only a few percent face-on,
  // stays dark either way.
  scene.background = new THREE.Color("#55575d");
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
  box(8, 4, 3.4, [0, 6, 1.5]); // overhead key
  box(2.4, 7, 2.6, [-5.5, 1, 2.5]); // left strip
  box(2.4, 7, 2.0, [5.5, 1, 1.5]); // right strip
  box(7, 2, 1.0, [0, -2.2, 5]); // low fill card
  box(4, 6, 1.6, [0, 1.5, -6]); // rim behind
  // A faint floor bounce, so undersides are not pure black.
  const floor = new THREE.Mesh(
    new THREE.PlaneGeometry(30, 30),
    new THREE.MeshBasicMaterial({ color: "#3a3b40" }),
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
    scene.environmentIntensity = 1.0;
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
          onCreated={({ gl }) => {
            // AgX: a filmic curve that rolls bright LEDs off toward white without
            // shifting their hue, the way a camera sensor does.
            gl.toneMapping = THREE.AgXToneMapping;
            gl.toneMappingExposure = 1.1;
          }}
        >
          <View.Port />
        </Canvas>
      )}
    </StageContext.Provider>
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
  const camera: [number, number, number] =
    framing === "head" ? [0, 0.1, 2.75] : level === "high" ? [0, 0.3, 4.0] : [0, 0.1, 2.9];
  const scene = (
    <>
      <PerspectiveCamera
        makeDefault
        position={camera}
        fov={30}
        onUpdate={(c) => c.lookAt(0, 0.04, 0)}
      />
      <Studio />
      {/* A neutral three-point studio rig: warm key, cool fill, white rim. */}
      <ambientLight intensity={0.15} />
      <directionalLight position={[2.2, 3.2, 2.8]} intensity={2.2} color="#fff6ec" />
      <directionalLight position={[-2.8, 0.8, 1.5]} intensity={0.6} color="#e8f0ff" />
      <directionalLight position={[0, 2, -3]} intensity={1.1} color="#ffffff" />
      <BotModel appearance={appearance} mood={mood} detail={level} phase={phase} />
    </>
  );
  if (standalone) {
    return (
      <span className={cx("botface", className)} style={{ width: size, height: size }}>
        <Canvas
          dpr={[1, 2]}
          gl={{ antialias: true, alpha: true }}
          onCreated={({ gl }) => {
            // AgX: a filmic curve that rolls bright LEDs off toward white without
            // shifting their hue, the way a camera sensor does.
            gl.toneMapping = THREE.AgXToneMapping;
            gl.toneMappingExposure = 1.1;
          }}
        >
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
