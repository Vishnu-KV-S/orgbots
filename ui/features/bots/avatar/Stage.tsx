"use client";

import { PerspectiveCamera, View } from "@react-three/drei";
import { Canvas, useThree } from "@react-three/fiber";
import { createContext, type CSSProperties, useContext, useEffect, useMemo, useState } from "react";
import * as THREE from "three";
import { RoomEnvironment } from "three/examples/jsm/environments/RoomEnvironment.js";
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
 * `RoomEnvironment` — no HDR file to download — once per renderer, shared by every
 * bot's scene.
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

function studioEnvironment(gl: THREE.WebGLRenderer): THREE.Texture {
  if (sharedEnvironment?.gl === gl) return sharedEnvironment.texture;
  const pmrem = new THREE.PMREMGenerator(gl);
  const texture = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
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
            gl.toneMapping = THREE.ACESFilmicToneMapping;
            gl.toneMappingExposure = 1.05;
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
      <ambientLight intensity={0.5} />
      <directionalLight position={[2.5, 3, 3]} intensity={1.7} />
      <directionalLight position={[-3, 1.2, -2]} intensity={0.9} color={appearance.glow} />
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
            gl.toneMapping = THREE.ACESFilmicToneMapping;
            gl.toneMappingExposure = 1.05;
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
