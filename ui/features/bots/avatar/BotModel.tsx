"use client";

import { RoundedBox } from "@react-three/drei";
import { useFrame } from "@react-three/fiber";
import { useEffect, useMemo, useRef } from "react";
import * as THREE from "three";
import type { Appearance, Shape } from "./appearance";
import { applyFace, cloneFace, easeFace, screenMaterial, targetFace } from "./eyes";
import type { Mood } from "./mood";
import { textures } from "./textures";

/**
 * One bot, finished like a consumer product: soft-touch shell, rubber gasket, satin
 * metal, one anodised accent in the bot's colour.
 *
 * **Every bot shares the finish and the eyes**; what differs is the shape, the top
 * piece, the shell colour and the accent colour. That is the look of a product
 * family rather than a toy box.
 *
 * **Nothing has a hard edge.** Shells are rounded boxes with large radii or turned
 * profiles, displays are rounded rectangles, and the hardware is domes and capsules.
 *
 * **The face is a display**: a dark screen in a rubber gasket under a satin glass
 * cover, with soft LED eyes drawn into it (`eyes.ts`). The eyes are the only light.
 *
 * The body animation is a pose function — mood and time in, target transform out —
 * damped every frame, so moods blend instead of snapping.
 */

/** Every bot's shell is pure white; the accent colour is what tells them apart. */
const SHELL = "#ffffff";

interface Layout {
  /** The screen mesh's vertical offset in the body; eye coordinates are relative to it. */
  screenY: number;
  eyeY: number;
  eyeX: number;
  topY: number;
  halfWidth: number;
  floorY: number;
}

const LAYOUT: Record<Shape, Layout> = {
  orb: { screenY: 0, eyeY: 0.02, eyeX: 0.15, topY: 0.6, halfWidth: 0.6, floorY: -0.62 },
  cube: { screenY: -0.02, eyeY: -0.02, eyeX: 0.15, topY: 0.475, halfWidth: 0.55, floorY: -0.5 },
  capsule: { screenY: 0.13, eyeY: 0.13, eyeX: 0.12, topY: 0.67, halfWidth: 0.42, floorY: -0.69 },
  pod: { screenY: -0.12, eyeY: -0.12, eyeX: 0.15, topY: 0.6, halfWidth: 0.55, floorY: -0.42 },
  tv: { screenY: 0, eyeY: 0, eyeX: 0.16, topY: 0.46, halfWidth: 0.6, floorY: -0.62 },
};

interface Pose {
  y: number;
  x: number;
  rotX: number;
  rotY: number;
  rotZ: number;
  sx: number;
  sy: number;
  spin: boolean;
}

const BASE: Pose = {
  y: 0,
  x: 0,
  rotX: 0,
  rotY: 0,
  rotZ: 0,
  sx: 1,
  sy: 1,
  spin: false,
};

/** Movements are small and weighted — a heavy object hovering, not a balloon. */
function pose(mood: Mood, t: number): Pose {
  const p: Pose = { ...BASE };
  switch (mood) {
    case "idle":
      p.y = 0.025 * Math.sin(t * 1.4);
      p.rotZ = 0.02 * Math.sin(t * 0.8);
      p.rotY = 0.16 * Math.sin(t * 0.3);
      break;
    case "thinking":
      p.y = 0.02 * Math.sin(t * 1.6);
      p.rotZ = 0.12 * Math.sin(t * 0.7);
      break;
    case "browsing":
      p.y = 0.015 * Math.sin(t * 2.2);
      p.rotY = 0.22 * Math.sin(t * 1.9);
      break;
    case "clicking": {
      const c = (t % 0.8) / 0.8;
      const hit = c < 0.14;
      p.sy = hit ? 0.93 : 1;
      p.sx = hit ? 1.04 : 1;
      p.y = hit ? -0.03 : 0.015 * Math.sin(c * Math.PI);
      p.rotX = hit ? 0.07 : 0;
      break;
    }
    case "typing":
      p.x = 0.004 * Math.sin(t * 55);
      p.rotZ = 0.01 * Math.sin(t * 41);
      p.y = 0.008 * Math.sin(t * 8);
      break;
    case "waiting":
      p.rotZ = 0.16 + 0.03 * Math.sin(t * 2.2);
      p.y = 0.015 * Math.sin(t * 1.6);
      break;
    case "error": {
      const env = Math.max(0, 1 - (t % 2) / 0.5);
      p.x = 0.035 * Math.sin(t * 38) * env;
      p.rotZ = -0.05;
      p.y = -0.015;
      break;
    }
    case "happy": {
      const c = (t % 1.8) / 1.8;
      p.y = c < 0.4 ? 0.12 * Math.sin((c / 0.4) * Math.PI) : 0;
      p.sy = c > 0.4 && c < 0.47 ? 0.95 : 1;
      p.sx = c > 0.4 && c < 0.47 ? 1.03 : 1;
      break;
    }
    case "creating":
      p.y = 0.04 + 0.02 * Math.sin(t * 3);
      p.spin = true;
      break;
    case "delegating":
      p.rotX = 0.1 + 0.05 * Math.sin(t * 4.5);
      p.rotY = 0.28;
      p.y = 0.01 * Math.sin(t * 4.5);
      break;
    case "remembering":
      p.rotX = 0.06 * Math.sin(t * 1.8);
      p.y = 0.015 * Math.sin(t * 1.8);
      break;
    case "sleeping":
      p.y = -0.03 + 0.012 * Math.sin(t * 1.0);
      p.sy = 1 + 0.01 * Math.sin(t * 1.0);
      p.rotZ = 0.08;
      break;
    case "stopped":
      p.y = -0.05;
      p.rotX = 0.2;
      break;
  }
  return p;
}

function useMaterials(a: Appearance) {
  const tex = textures();
  const materials = useMemo(() => {
    // Soft-touch shell: matte, a fine grain, a little velvet at grazing angles.
    const shell = new THREE.MeshPhysicalMaterial({
      color: SHELL,
      roughness: 0.58,
      roughnessMap: tex.plasticRough,
      bumpMap: tex.softGrain,
      bumpScale: 0.35,
      sheen: 0.35,
      sheenRoughness: 0.8,
      sheenColor: new THREE.Color("#ffffff"),
      clearcoat: 0.06,
      clearcoatRoughness: 0.6,
    });
    // Graphite rubber: the gasket round the display, the base, the ears.
    const rubber = new THREE.MeshPhysicalMaterial({
      color: "#2a2a2d",
      roughness: 0.82,
      bumpMap: tex.softGrain,
      bumpScale: 0.5,
      sheen: 0.2,
      sheenRoughness: 0.9,
      sheenColor: new THREE.Color("#ffffff"),
    });
    // Satin dark metal, with a trace of brush grain.
    const metal = new THREE.MeshPhysicalMaterial({
      color: "#6e6a65",
      metalness: 0.9,
      roughness: 0.34,
      roughnessMap: tex.brushedRough,
    });
    // One anodised accent in the bot's colour, like the ring round a lens.
    const accent = new THREE.MeshPhysicalMaterial({
      color: a.glow,
      metalness: 0.45,
      roughness: 0.38,
      clearcoat: 0.3,
      clearcoatRoughness: 0.3,
    });
    // Satin cover glass: dark, softly reflective.
    const glass = new THREE.MeshPhysicalMaterial({
      color: "#0e0e10",
      roughness: 0.14,
      clearcoat: 0.5,
      clearcoatRoughness: 0.15,
      envMapIntensity: 0.5,
      transparent: true,
      opacity: 0.22,
      depthWrite: false,
    });
    // The display itself: soft LED eyes, drawn by a shader (`eyes.ts`).
    const screen = screenMaterial(a);
    const seam = new THREE.MeshStandardMaterial({ color: "#1d1d1f", roughness: 0.8 });
    return { shell, rubber, metal, accent, glass, screen, seam };
  }, [a.glow, tex]);

  useEffect(() => () => Object.values(materials).forEach((m) => m.dispose()), [materials]);
  return materials;
}

type Mats = ReturnType<typeof useMaterials>;

/** A flat panel with rounded corners — a display, its gasket, the glass over it.
 * (`RoundedBox` cannot round corners beyond half its depth, which for a thin panel
 * is no rounding at all.) Centred, with its front face at z = depth / 2. */
const panels = new Map<string, THREE.ExtrudeGeometry>();
function panel(w: number, h: number, r: number, depth: number): THREE.ExtrudeGeometry {
  const key = `${w}:${h}:${r}:${depth}`;
  const hit = panels.get(key);
  if (hit) return hit;
  const shape = new THREE.Shape();
  const x = -w / 2;
  const y = -h / 2;
  shape.moveTo(x + r, y);
  shape.lineTo(x + w - r, y);
  shape.quadraticCurveTo(x + w, y, x + w, y + r);
  shape.lineTo(x + w, y + h - r);
  shape.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
  shape.lineTo(x + r, y + h);
  shape.quadraticCurveTo(x, y + h, x, y + h - r);
  shape.lineTo(x, y + r);
  shape.quadraticCurveTo(x, y, x + r, y);
  const bevel = Math.min(0.006, depth / 2.5);
  const geometry = new THREE.ExtrudeGeometry(shape, {
    depth,
    bevelEnabled: true,
    bevelThickness: bevel,
    bevelSize: bevel,
    bevelSegments: 4,
    curveSegments: 16,
  });
  geometry.translate(0, 0, -depth / 2);
  panels.set(key, geometry);
  return geometry;
}

/** The pod: one turned profile — dome, gently tapered sides, a rounded foot. */
let podGeometry: THREE.LatheGeometry | null = null;
function pod(): THREE.LatheGeometry {
  if (podGeometry) return podGeometry;
  const pts: THREE.Vector2[] = [new THREE.Vector2(0, -0.41)];
  for (let i = 0; i <= 8; i++) {
    const a = -Math.PI / 2 + (i / 8) * (Math.PI / 2);
    pts.push(new THREE.Vector2(0.42 + 0.08 * Math.cos(a), -0.33 + 0.08 * Math.sin(a)));
  }
  pts.push(new THREE.Vector2(0.55, 0.05));
  for (let i = 1; i <= 24; i++) {
    const a = (i / 24) * (Math.PI / 2);
    pts.push(new THREE.Vector2(Math.max(0.0001, 0.55 * Math.cos(a)), 0.05 + 0.55 * Math.sin(a)));
  }
  podGeometry = new THREE.LatheGeometry(pts, 96);
  return podGeometry;
}

/** A rounded rectangular display in a rubber gasket, under glass. */
function FlatFace({
  m,
  w,
  h,
  r,
  z,
  y,
}: {
  m: Mats;
  w: number;
  h: number;
  r: number;
  z: number;
  y: number;
}) {
  return (
    <group position={[0, y, 0]}>
      <mesh
        geometry={panel(w + 0.07, h + 0.07, r + 0.035, 0.014)}
        position={[0, 0, z]}
        material={m.rubber}
      />
      <mesh geometry={panel(w, h, r, 0.01)} position={[0, 0, z + 0.008]} material={m.screen} />
      <mesh
        geometry={panel(w + 0.01, h + 0.01, r + 0.005, 0.006)}
        position={[0, 0, z + 0.02]}
        material={m.glass}
        renderOrder={2}
      />
    </group>
  );
}

function Body({ shape, m }: { shape: Shape; m: Mats }) {
  switch (shape) {
    case "orb":
      return (
        <group>
          <mesh material={m.shell}>
            <sphereGeometry args={[0.6, 96, 64]} />
          </mesh>
          {/* Gasket, display, glass: patches of concentric spheres across the front. */}
          <mesh material={m.rubber}>
            <sphereGeometry args={[0.6015, 80, 48, Math.PI / 2 - 0.88, 1.76, 0.97, 1.1]} />
          </mesh>
          <mesh material={m.screen}>
            <sphereGeometry args={[0.603, 80, 48, Math.PI / 2 - 0.8, 1.6, 1.04, 0.96]} />
          </mesh>
          <mesh material={m.glass} renderOrder={2}>
            <sphereGeometry args={[0.618, 80, 48, Math.PI / 2 - 0.82, 1.64, 1.02, 1.0]} />
          </mesh>
          <mesh material={m.seam} rotation={[Math.PI / 2, 0, 0]} position={[0, 0.36, 0]}>
            <torusGeometry args={[0.48, 0.003, 6, 96]} />
          </mesh>
        </group>
      );
    case "cube":
      return (
        <group>
          <RoundedBox args={[1.1, 0.95, 1.0]} radius={0.2} smoothness={10} material={m.shell} />
          <FlatFace m={m} w={0.62} h={0.44} r={0.15} z={0.5} y={-0.02} />
        </group>
      );
    case "capsule":
      return (
        <group>
          <mesh material={m.shell}>
            <capsuleGeometry args={[0.42, 0.5, 24, 64]} />
          </mesh>
          <mesh material={m.rubber} position={[0, 0.13, 0]}>
            <cylinderGeometry args={[0.4215, 0.4215, 0.38, 64, 1, true, -1.0, 2.0]} />
          </mesh>
          <mesh material={m.screen} position={[0, 0.13, 0]}>
            <cylinderGeometry args={[0.423, 0.423, 0.32, 64, 1, true, -0.92, 1.84]} />
          </mesh>
          <mesh material={m.glass} position={[0, 0.13, 0]} renderOrder={2}>
            <cylinderGeometry args={[0.44, 0.44, 0.34, 64, 1, true, -0.95, 1.9]} />
          </mesh>
          <mesh material={m.seam} rotation={[Math.PI / 2, 0, 0]} position={[0, -0.18, 0]}>
            <torusGeometry args={[0.42, 0.003, 6, 96]} />
          </mesh>
        </group>
      );
    case "pod":
      return (
        <group>
          <mesh material={m.shell} geometry={pod()} />
          <mesh material={m.rubber} position={[0, -0.12, 0]}>
            <cylinderGeometry args={[0.5485, 0.5105, 0.29, 80, 1, true, -1.05, 2.1]} />
          </mesh>
          <mesh material={m.screen} position={[0, -0.12, 0]}>
            <cylinderGeometry args={[0.5464, 0.5148, 0.24, 80, 1, true, -0.98, 1.96]} />
          </mesh>
          <mesh material={m.glass} position={[0, -0.12, 0]} renderOrder={2}>
            <cylinderGeometry args={[0.564, 0.532, 0.26, 80, 1, true, -1.0, 2.0]} />
          </mesh>
          <mesh material={m.seam} rotation={[Math.PI / 2, 0, 0]} position={[0, 0.05, 0]}>
            <torusGeometry args={[0.551, 0.003, 6, 96]} />
          </mesh>
        </group>
      );
    case "tv":
      return (
        <group>
          <RoundedBox args={[1.2, 0.92, 0.8]} radius={0.18} smoothness={10} material={m.shell} />
          <FlatFace m={m} w={0.72} h={0.46} r={0.14} z={0.4} y={0} />
          {[-1, 1].map((s) => (
            <mesh
              key={s}
              material={m.metal}
              position={[s * 0.34, -0.53, 0]}
              rotation={[0, 0, s * 0.22]}
            >
              <capsuleGeometry args={[0.028, 0.14, 8, 20]} />
            </mesh>
          ))}
        </group>
      );
  }
}

function TopPiece({ a, m, layout }: { a: Appearance; m: Mats; layout: Layout }) {
  const wobble = useRef<THREE.Group>(null);
  useFrame((state) => {
    if (wobble.current) wobble.current.rotation.z = 0.05 * Math.sin(state.clock.elapsedTime * 2.6);
  });
  const y = layout.topY;
  switch (a.top) {
    case "ring":
      // A lens-like button: satin metal bezel, the accent ring, a rubber centre.
      return (
        <group position={[0, y - 0.02, 0]}>
          <mesh material={m.metal} rotation={[Math.PI / 2, 0, 0]}>
            <torusGeometry args={[0.125, 0.022, 24, 64]} />
          </mesh>
          <mesh material={m.accent} rotation={[Math.PI / 2, 0, 0]} position={[0, 0.006, 0]}>
            <torusGeometry args={[0.096, 0.012, 20, 64]} />
          </mesh>
          <mesh material={m.rubber} position={[0, 0.004, 0]} scale={[1, 0.28, 1]}>
            <sphereGeometry args={[0.085, 40, 20]} />
          </mesh>
        </group>
      );
    case "knobs":
      return (
        <group position={[0, y + 0.02, 0]}>
          {[-1, 1].map((s) => (
            <group key={s} position={[s * 0.26, 0, 0]}>
              <mesh material={m.metal}>
                <capsuleGeometry args={[0.06, 0.06, 12, 32]} />
              </mesh>
              <mesh material={m.accent} position={[0, 0.085, 0]} scale={[1, 0.6, 1]}>
                <sphereGeometry args={[0.04, 24, 16]} />
              </mesh>
            </group>
          ))}
        </group>
      );
    case "antenna":
      return (
        <group ref={wobble} position={[0, y - 0.02, 0]}>
          <mesh material={m.rubber} scale={[1, 0.5, 1]}>
            <sphereGeometry args={[0.055, 32, 16]} />
          </mesh>
          <mesh material={m.metal} position={[0, 0.15, 0]}>
            <capsuleGeometry args={[0.01, 0.26, 6, 12]} />
          </mesh>
          <mesh material={m.accent} position={[0, 0.3, 0]}>
            <sphereGeometry args={[0.03, 24, 16]} />
          </mesh>
        </group>
      );
    case "ears":
      return (
        <group>
          {[-1, 1].map((s) => (
            <group key={s} position={[s * (layout.halfWidth + 0.02), 0.05, 0]}>
              <RoundedBox
                args={[0.07, 0.26, 0.22]}
                radius={0.034}
                smoothness={6}
                material={m.rubber}
              />
              <mesh material={m.accent} position={[s * 0.036, 0, 0]} rotation={[0, 0, Math.PI / 2]}>
                <capsuleGeometry args={[0.012, 0.12, 6, 12]} />
              </mesh>
            </group>
          ))}
        </group>
      );
    case "halo":
      // A sensor ring on a short mast.
      return (
        <group position={[0, y - 0.02, 0]}>
          <mesh material={m.metal} position={[0, 0.07, 0]}>
            <capsuleGeometry args={[0.018, 0.1, 6, 16]} />
          </mesh>
          <mesh material={m.metal} position={[0, 0.15, 0]} rotation={[Math.PI / 2, 0, 0]}>
            <torusGeometry args={[0.1, 0.016, 20, 64]} />
          </mesh>
          <mesh material={m.accent} position={[0, 0.15, 0]} rotation={[Math.PI / 2, 0, 0]}>
            <torusGeometry args={[0.1, 0.006, 12, 64]} />
          </mesh>
        </group>
      );
    case "none":
      return null;
  }
}

function ContactShadow({ y }: { y: number }) {
  const tex = textures();
  return (
    <mesh rotation={[-Math.PI / 2, 0, 0]} position={[0, y, 0]} renderOrder={-1}>
      <planeGeometry args={[1.5, 1.5]} />
      <meshBasicMaterial map={tex.shadow} transparent depthWrite={false} toneMapped={false} />
    </mesh>
  );
}

export function BotModel({
  appearance,
  mood,
  detail = "high",
  phase = 0,
}: {
  appearance: Appearance;
  mood: Mood;
  /** `low` for list-sized avatars: no shadow plane. */
  detail?: "high" | "low";
  /** Offsets this bot's clock so a list of bots does not move in lockstep. */
  phase?: number;
}) {
  const layout = LAYOUT[appearance.shape];
  const m = useMaterials(appearance);
  const root = useRef<THREE.Group>(null);
  const body = useRef<THREE.Group>(null);
  // The face being shown, eased toward the mood's target every frame.
  const face = useRef(cloneFace(targetFace(mood, layout.eyeX, 0, phase)));
  const spin = useRef(0);
  const sway = useRef(0);

  useFrame((state, dt) => {
    const t = state.clock.elapsedTime + phase;
    const p = pose(mood, t);
    const k = Math.min(1, dt * 8);
    const r = root.current;
    const b = body.current;
    if (!r || !b) return;

    r.position.y += (p.y - r.position.y) * k;
    r.position.x += (p.x - r.position.x) * Math.min(1, dt * 30);
    r.rotation.x += (p.rotX - r.rotation.x) * k;
    r.rotation.z += (p.rotZ - r.rotation.z) * k;

    if (p.spin) {
      spin.current += dt * 3.2;
    } else {
      const target = Math.round(spin.current / (Math.PI * 2)) * Math.PI * 2;
      spin.current += (target - spin.current) * Math.min(1, dt * 4);
    }
    sway.current += (p.rotY - sway.current) * k;
    r.rotation.y = spin.current + sway.current;

    b.scale.x += (p.sx - b.scale.x) * Math.min(1, dt * 16);
    b.scale.z = b.scale.x;
    b.scale.y += (p.sy - b.scale.y) * Math.min(1, dt * 16);

    // The display: ease the eyes toward this moment's expression and draw it.
    easeFace(face.current, targetFace(mood, layout.eyeX, t, phase * 7), dt);
    applyFace(m.screen, face.current, 0, layout.eyeY - layout.screenY);
  });

  return (
    <group>
      <group ref={root}>
        <group ref={body}>
          {/* Keyed so a shape change remounts the parts rather than letting React reuse
              one shape's meshes for another's: a mesh that had its geometry as a prop
              and is reused for one that declares it as a child ends up with none. */}
          <Body key={appearance.shape} shape={appearance.shape} m={m} />
          <TopPiece
            key={`${appearance.top}:${appearance.shape}`}
            a={appearance}
            m={m}
            layout={layout}
          />
        </group>
      </group>
      {detail === "high" && <ContactShadow y={layout.floorY} />}
    </group>
  );
}
