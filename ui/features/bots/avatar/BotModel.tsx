"use client";

import { RoundedBox } from "@react-three/drei";
import { useFrame } from "@react-three/fiber";
import { useEffect, useMemo, useRef } from "react";
import * as THREE from "three";
import { RoundedBoxGeometry } from "three/examples/jsm/geometries/RoundedBoxGeometry.js";
import type { Appearance, Shape } from "./appearance";
import type { Mood } from "./mood";
import { textures } from "./textures";

/**
 * One bot, built like a product rather than a cartoon.
 *
 * **The face is three layers**, the way a real device's display is: an opaque black
 * screen recessed into the shell, LED eyes on that screen, and a smoked-glass cover
 * in front of both. The glass takes the reflections; the eyes glow from underneath
 * it, with a hot core falling off to the rim. Nothing floats around the bot — no
 * particles, rings or symbols. Mood is carried by the body and the eyes alone.
 *
 * **Surfaces have texture**: grain on plastic, brush lines on aluminium, faint smudges
 * on the glass, recessed seams and real screw heads. See `textures.ts`.
 *
 * The animation is a pose function — mood and time in, target transform out — damped
 * every frame, so moods blend instead of snapping.
 */

interface Layout {
  /** Where the eyes sit on a flat screen. */
  eyeZ: number;
  /** A curved screen's radius; `sphere` if it curves vertically too. */
  radius?: number;
  sphere?: boolean;
  eyeY: number;
  eyeX: number;
  topY: number;
  halfWidth: number;
  floorY: number;
}

const LAYOUT: Record<Shape, Layout> = {
  orb: {
    eyeZ: 0,
    radius: 0.603,
    sphere: true,
    eyeY: 0.02,
    eyeX: 0.165,
    topY: 0.6,
    halfWidth: 0.6,
    floorY: -0.62,
  },
  cube: { eyeZ: 0.516, eyeY: -0.02, eyeX: 0.19, topY: 0.475, halfWidth: 0.55, floorY: -0.5 },
  capsule: {
    eyeZ: 0,
    radius: 0.423,
    eyeY: 0.13,
    eyeX: 0.13,
    topY: 0.67,
    halfWidth: 0.42,
    floorY: -0.69,
  },
  pod: {
    eyeZ: 0,
    radius: 0.552,
    eyeY: -0.12,
    eyeX: 0.17,
    topY: 0.6,
    halfWidth: 0.56,
    floorY: -0.46,
  },
  tv: { eyeZ: 0.416, eyeY: 0.0, eyeX: 0.2, topY: 0.46, halfWidth: 0.6, floorY: -0.64 },
};

const ERROR_LED = new THREE.Color("#ff3b30");

interface Pose {
  y: number;
  x: number;
  rotX: number;
  rotY: number;
  rotZ: number;
  sx: number;
  sy: number;
  lookX: number;
  lookY: number;
  /** 1 open, 0 shut. */
  open: number;
  /** LED brightness multiplier. */
  glow: number;
  left: number;
  right: number;
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
  lookX: 0,
  lookY: 0,
  open: 1,
  glow: 1,
  left: 1,
  right: 1,
  spin: false,
};

const blinkAt = (t: number, every: number) => (t % every < 0.12 ? 0.06 : 1);

/** Movements are small and weighted — a heavy object hovering, not a balloon. */
function pose(mood: Mood, t: number): Pose {
  const p: Pose = { ...BASE };
  switch (mood) {
    case "idle":
      p.y = 0.025 * Math.sin(t * 1.4);
      p.rotZ = 0.02 * Math.sin(t * 0.8);
      p.rotY = 0.16 * Math.sin(t * 0.3);
      p.lookX = 0.03 * Math.tanh(4 * Math.sin(t * 0.42));
      p.open = blinkAt(t, 4.1);
      break;
    case "thinking":
      p.y = 0.02 * Math.sin(t * 1.6);
      p.rotZ = 0.12 * Math.sin(t * 0.7);
      p.lookX = 0.03 + 0.008 * Math.sin(t * 1.3);
      p.lookY = 0.03;
      p.glow = 0.85 + 0.2 * Math.sin(t * 2.4);
      p.open = blinkAt(t, 3.1);
      break;
    case "browsing":
      p.y = 0.015 * Math.sin(t * 2.2);
      p.lookX = 0.05 * Math.sin(t * 1.9);
      p.rotY = 0.22 * Math.sin(t * 1.9);
      p.open = blinkAt(t, 2.6);
      break;
    case "clicking": {
      const c = (t % 0.8) / 0.8;
      const hit = c < 0.14;
      p.sy = hit ? 0.93 : 1;
      p.sx = hit ? 1.04 : 1;
      p.y = hit ? -0.03 : 0.015 * Math.sin(c * Math.PI);
      p.rotX = hit ? 0.07 : 0;
      p.open = hit ? 0.5 : 1;
      p.lookY = -0.02;
      break;
    }
    case "typing":
      p.x = 0.004 * Math.sin(t * 55);
      p.rotZ = 0.01 * Math.sin(t * 41);
      p.y = 0.008 * Math.sin(t * 8);
      p.lookY = -0.03;
      p.open = 0.55;
      break;
    case "waiting":
      p.rotZ = 0.16 + 0.03 * Math.sin(t * 2.2);
      p.y = 0.015 * Math.sin(t * 1.6);
      p.left = 1.18;
      p.right = 0.85;
      p.lookY = 0.02;
      p.glow = 1 + 0.25 * Math.max(0, Math.sin(t * 3));
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
      p.glow = 1.2;
      break;
    case "delegating":
      p.rotX = 0.1 + 0.05 * Math.sin(t * 4.5);
      p.rotY = 0.28;
      p.y = 0.01 * Math.sin(t * 4.5);
      p.lookX = 0.04;
      p.open = blinkAt(t, 1.6);
      break;
    case "remembering":
      p.rotX = 0.06 * Math.sin(t * 1.8);
      p.y = 0.015 * Math.sin(t * 1.8);
      p.glow = 1.15 + 0.25 * Math.sin(t * 2.4);
      break;
    case "sleeping":
      p.y = -0.03 + 0.012 * Math.sin(t * 1.0);
      p.sy = 1 + 0.01 * Math.sin(t * 1.0);
      p.rotZ = 0.08;
      p.glow = 0.3;
      break;
    case "stopped":
      p.y = -0.05;
      p.rotX = 0.2;
      p.glow = 0.12;
      break;
  }
  return p;
}

type EyeKind = "pill" | "round" | "square" | "visor" | "dot" | "arc" | "x" | "line";

function eyeKind(base: Appearance["eyes"], mood: Mood): EyeKind {
  if (mood === "happy") return "arc";
  if (mood === "error") return "x";
  if (mood === "sleeping" || mood === "remembering" || mood === "stopped") return "line";
  return base;
}

function useMaterials(a: Appearance, mood: Mood) {
  const tex = textures();
  const materials = useMemo(() => {
    const base = new THREE.Color(a.body);
    let body: THREE.MeshPhysicalMaterial;
    switch (a.finish) {
      case "metal":
        // Anodised, brushed aluminium.
        body = new THREE.MeshPhysicalMaterial({
          color: base,
          metalness: 1,
          roughness: 0.42,
          roughnessMap: tex.brushedRough,
          clearcoat: 0.15,
          clearcoatRoughness: 0.4,
        });
        break;
      case "matte":
        // Soft-touch plastic.
        body = new THREE.MeshPhysicalMaterial({
          color: base,
          roughness: 0.78,
          roughnessMap: tex.plasticRough,
          bumpMap: tex.matteBump,
          bumpScale: 0.6,
          sheen: 0.25,
          sheenRoughness: 0.8,
          sheenColor: new THREE.Color("#ffffff"),
        });
        break;
      case "pearl":
        // Glazed ceramic: a deep clear glaze over a soft base.
        body = new THREE.MeshPhysicalMaterial({
          color: base,
          roughness: 0.35,
          roughnessMap: tex.plasticRough,
          clearcoat: 1,
          clearcoatRoughness: 0.06,
          sheen: 0.3,
          sheenRoughness: 0.5,
          sheenColor: new THREE.Color("#ffffff"),
        });
        break;
      default:
        // Gloss injection-moulded plastic.
        body = new THREE.MeshPhysicalMaterial({
          color: base,
          roughness: 0.32,
          roughnessMap: tex.plasticRough,
          bumpMap: tex.plasticBump,
          bumpScale: 0.25,
          clearcoat: 0.7,
          clearcoatRoughness: 0.18,
        });
    }
    // Smoked glass over the display: dark, very glossy, faintly smudged.
    const glass = new THREE.MeshPhysicalMaterial({
      color: "#050506",
      metalness: 0,
      roughness: 0.14,
      roughnessMap: tex.smudge,
      clearcoat: 0.6,
      clearcoatRoughness: 0.12,
      envMapIntensity: 0.3,
      transparent: true,
      opacity: 0.38,
      depthWrite: false,
    });
    // The display behind it: matte black, so the LEDs are the only light there.
    const screen = new THREE.MeshStandardMaterial({
      color: new THREE.Color("#020203").lerp(new THREE.Color(a.glow), 0.012),
      roughness: 0.9,
    });
    // LEDs: emissive with a centre-bright falloff; tone mapping turns the hot core
    // toward white the way a camera does with a bright light.
    // Drawn after the glass (transparent + renderOrder), so the light reads through
    // it at full strength — a lit LED dominates the cover in front of it — while the
    // depth test still hides it behind the shell when the bot turns away.
    const led = new THREE.MeshStandardMaterial({
      color: "#000000",
      emissive: new THREE.Color(a.glow),
      emissiveMap: tex.led,
      emissiveIntensity: 2.4,
      roughness: 0.4,
      transparent: true,
      opacity: 1,
    });
    const steel = new THREE.MeshPhysicalMaterial({
      color: "#b9bcc4",
      metalness: 1,
      roughness: 0.3,
      roughnessMap: tex.brushedRough,
    });
    // Recessed seams: a near-black gap, not a decorative stripe.
    const seam = new THREE.MeshStandardMaterial({ color: "#0b0b0d", roughness: 0.8 });
    const rubber = new THREE.MeshStandardMaterial({ color: "#1a1a1d", roughness: 0.92 });
    return { body, glass, screen, led, steel, seam, rubber };
  }, [a.body, a.glow, a.finish, tex]);

  useEffect(() => () => Object.values(materials).forEach((m) => m.dispose()), [materials]);

  useEffect(() => {
    materials.led.emissive.set(mood === "error" ? ERROR_LED : a.glow);
  }, [materials, mood, a.glow]);

  return materials;
}

const DEPTH = 0.012;
const EYE_GEOMETRY = {
  pill: new RoundedBoxGeometry(0.11, 0.19, DEPTH, 4, 0.005),
  round: new THREE.CylinderGeometry(0.08, 0.08, DEPTH, 40).rotateX(Math.PI / 2),
  square: new RoundedBoxGeometry(0.16, 0.14, DEPTH, 4, 0.005),
  visor: new RoundedBoxGeometry(0.48, 0.085, DEPTH, 4, 0.005),
  dot: new THREE.CylinderGeometry(0.048, 0.048, DEPTH, 32).rotateX(Math.PI / 2),
  arc: new THREE.TorusGeometry(0.07, 0.018, 10, 36, Math.PI).scale(1, 1, 0.4),
  bar: new THREE.BoxGeometry(0.15, 0.028, DEPTH),
  line: new RoundedBoxGeometry(0.14, 0.026, DEPTH, 2, 0.005),
};

function Eye({ kind, material }: { kind: EyeKind; material: THREE.Material }) {
  if (kind === "x") {
    return (
      <group>
        <mesh
          geometry={EYE_GEOMETRY.bar}
          material={material}
          rotation={[0, 0, Math.PI / 4]}
          renderOrder={3}
        />
        <mesh
          geometry={EYE_GEOMETRY.bar}
          material={material}
          rotation={[0, 0, -Math.PI / 4]}
          renderOrder={3}
        />
      </group>
    );
  }
  const geometry =
    kind === "arc" ? EYE_GEOMETRY.arc : kind === "line" ? EYE_GEOMETRY.line : EYE_GEOMETRY[kind];
  return <mesh geometry={geometry} material={material} renderOrder={3} />;
}

type Mats = ReturnType<typeof useMaterials>;

/** A flat panel with properly rounded corners — a display, or the glass over one.
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
  const geometry = new THREE.ExtrudeGeometry(shape, {
    depth,
    bevelEnabled: true,
    bevelThickness: Math.min(0.004, depth / 3),
    bevelSize: 0.004,
    bevelSegments: 3,
    curveSegments: 12,
  });
  geometry.translate(0, 0, -depth / 2);
  panels.set(key, geometry);
  return geometry;
}

/** A screw head with a slot. */
function Screw({ m, position }: { m: Mats; position: [number, number, number] }) {
  return (
    <group position={position}>
      <mesh material={m.steel} rotation={[Math.PI / 2, 0, 0]}>
        <cylinderGeometry args={[0.024, 0.026, 0.008, 20]} />
      </mesh>
      <mesh material={m.seam} position={[0, 0, 0.0045]} rotation={[0, 0, 0.6]}>
        <boxGeometry args={[0.032, 0.006, 0.002]} />
      </mesh>
    </group>
  );
}

function Body({ shape, m }: { shape: Shape; m: Mats }) {
  switch (shape) {
    case "orb":
      return (
        <group>
          <mesh material={m.body}>
            <sphereGeometry args={[0.6, 96, 64]} />
          </mesh>
          {/* Screen, then glass: patches of concentric spheres across the front. */}
          <mesh material={m.screen}>
            <sphereGeometry args={[0.603, 80, 48, Math.PI / 2 - 0.8, 1.6, 1.04, 0.96]} />
          </mesh>
          <mesh material={m.glass} renderOrder={2}>
            <sphereGeometry args={[0.627, 80, 48, Math.PI / 2 - 0.82, 1.64, 1.02, 1.0]} />
          </mesh>
          <mesh material={m.seam} rotation={[Math.PI / 2, 0, 0]} position={[0, 0.33, 0]}>
            <torusGeometry args={[0.503, 0.004, 6, 96]} />
          </mesh>
        </group>
      );
    case "cube":
      return (
        <group>
          <RoundedBox args={[1.1, 0.95, 1.0]} radius={0.16} smoothness={8} material={m.body} />
          <mesh
            geometry={panel(0.86, 0.62, 0.09, 0.02)}
            position={[0, -0.02, 0.5]}
            material={m.screen}
          />
          <mesh
            geometry={panel(0.88, 0.64, 0.09, 0.008)}
            position={[0, -0.02, 0.532]}
            material={m.glass}
            renderOrder={2}
          />

          {(
            [
              [-0.43, 0.36],
              [0.43, 0.36],
              [-0.43, -0.38],
              [0.43, -0.38],
            ] as const
          ).map(([x, y]) => (
            <Screw key={`${x}${y}`} m={m} position={[x, y, 0.5]} />
          ))}
        </group>
      );
    case "capsule":
      return (
        <group>
          <mesh material={m.body}>
            <capsuleGeometry args={[0.42, 0.5, 24, 64]} />
          </mesh>
          <mesh material={m.screen} position={[0, 0.13, 0]}>
            <cylinderGeometry args={[0.423, 0.423, 0.32, 64, 1, true, -0.92, 1.84]} />
          </mesh>
          <mesh material={m.glass} position={[0, 0.13, 0]} renderOrder={2}>
            <cylinderGeometry args={[0.447, 0.447, 0.35, 64, 1, true, -0.96, 1.92]} />
          </mesh>
          <mesh material={m.seam} rotation={[Math.PI / 2, 0, 0]} position={[0, -0.16, 0]}>
            <torusGeometry args={[0.42, 0.004, 6, 96]} />
          </mesh>
        </group>
      );
    case "pod":
      return (
        <group>
          <mesh material={m.body} position={[0, 0.05, 0]}>
            <sphereGeometry args={[0.55, 80, 40, 0, Math.PI * 2, 0, Math.PI / 2]} />
          </mesh>
          <mesh material={m.body} position={[0, -0.2, 0]}>
            <cylinderGeometry args={[0.55, 0.5, 0.5, 80]} />
          </mesh>
          <mesh material={m.screen} position={[0, -0.12, 0]}>
            <cylinderGeometry args={[0.554, 0.53, 0.24, 64, 1, true, -0.98, 1.96]} />
          </mesh>
          <mesh material={m.glass} position={[0, -0.12, 0]} renderOrder={2}>
            <cylinderGeometry args={[0.58, 0.556, 0.27, 64, 1, true, -1.02, 2.04]} />
          </mesh>
          <mesh material={m.seam} rotation={[Math.PI / 2, 0, 0]} position={[0, 0.05, 0]}>
            <torusGeometry args={[0.551, 0.004, 6, 96]} />
          </mesh>
          <mesh material={m.rubber} position={[0, -0.455, 0]}>
            <cylinderGeometry args={[0.5, 0.47, 0.02, 64]} />
          </mesh>
        </group>
      );
    case "tv":
      return (
        <group>
          <RoundedBox args={[1.2, 0.92, 0.8]} radius={0.12} smoothness={8} material={m.body} />
          <mesh
            geometry={panel(0.9, 0.66, 0.12, 0.02)}
            position={[0, 0, 0.4]}
            material={m.screen}
          />
          <mesh
            geometry={panel(0.92, 0.68, 0.12, 0.008)}
            position={[0, 0, 0.432]}
            material={m.glass}
            renderOrder={2}
          />
          {[-1, 1].map((s) => (
            <group key={s} position={[s * 0.36, -0.53, 0]} rotation={[0, 0, s * 0.22]}>
              <mesh material={m.steel}>
                <cylinderGeometry args={[0.03, 0.022, 0.18, 16]} />
              </mesh>
              <mesh material={m.rubber} position={[0, -0.095, 0]}>
                <sphereGeometry args={[0.028, 16, 10]} />
              </mesh>
            </group>
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
      // A recessed button with a steel bezel.
      return (
        <group position={[0, y - 0.025, 0]}>
          <mesh material={m.steel} rotation={[Math.PI / 2, 0, 0]}>
            <torusGeometry args={[0.13, 0.022, 20, 64]} />
          </mesh>
          <mesh material={m.body} position={[0, 0.004, 0]}>
            <cylinderGeometry args={[0.11, 0.11, 0.03, 48]} />
          </mesh>
        </group>
      );
    case "knobs":
      return (
        <group position={[0, y, 0]}>
          {[-1, 1].map((s) => (
            <group key={s} position={[s * 0.26, 0.04, 0]}>
              <mesh material={m.steel}>
                <cylinderGeometry args={[0.07, 0.085, 0.08, 40]} />
              </mesh>
              <mesh material={m.steel} position={[0, 0.07, 0]}>
                <cylinderGeometry args={[0.04, 0.045, 0.07, 32]} />
              </mesh>
              <mesh material={m.seam} position={[0, 0.106, 0]}>
                <cylinderGeometry args={[0.025, 0.025, 0.002, 24]} />
              </mesh>
            </group>
          ))}
        </group>
      );
    case "antenna":
      return (
        <group ref={wobble} position={[0, y - 0.02, 0]}>
          <mesh material={m.rubber} position={[0, 0.015, 0]}>
            <cylinderGeometry args={[0.045, 0.055, 0.03, 32]} />
          </mesh>
          <mesh material={m.steel} position={[0, 0.16, 0]}>
            <cylinderGeometry args={[0.009, 0.012, 0.28, 12]} />
          </mesh>
          <mesh material={m.led} position={[0, 0.31, 0]}>
            <sphereGeometry args={[0.028, 24, 16]} />
          </mesh>
        </group>
      );
    case "ears":
      return (
        <group>
          {[-1, 1].map((s) => (
            <group key={s} position={[s * (layout.halfWidth + 0.015), 0.05, 0]}>
              <RoundedBox
                args={[0.06, 0.26, 0.22]}
                radius={0.025}
                smoothness={4}
                material={m.rubber}
              />
              {[-0.06, 0, 0.06].map((z) => (
                <mesh key={z} material={m.seam} position={[s * 0.031, 0, z]}>
                  <boxGeometry args={[0.002, 0.18, 0.012]} />
                </mesh>
              ))}
            </group>
          ))}
        </group>
      );
    case "halo":
      // A sensor ring on a short mast: the realistic reading of a halo.
      return (
        <group position={[0, y - 0.02, 0]}>
          <mesh material={m.steel} position={[0, 0.06, 0]}>
            <cylinderGeometry args={[0.018, 0.022, 0.12, 16]} />
          </mesh>
          <mesh material={m.steel} position={[0, 0.13, 0]} rotation={[Math.PI / 2, 0, 0]}>
            <torusGeometry args={[0.1, 0.014, 16, 64]} />
          </mesh>
          <mesh material={m.led} position={[0, 0.13, 0]} rotation={[Math.PI / 2, 0, 0]}>
            <torusGeometry args={[0.1, 0.004, 8, 64]} />
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
  const m = useMaterials(appearance, mood);
  const root = useRef<THREE.Group>(null);
  const body = useRef<THREE.Group>(null);
  const eyes = useRef<THREE.Group>(null);
  const leftEye = useRef<THREE.Group>(null);
  const rightEye = useRef<THREE.Group>(null);
  const spin = useRef(0);
  const sway = useRef(0);
  const kind = eyeKind(appearance.eyes, mood);
  const single = kind === "visor" || (appearance.eyes === "visor" && kind === "line");

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

    const e = eyes.current;
    if (e) {
      e.position.x += (p.lookX - e.position.x) * k;
      e.position.y += (layout.eyeY + p.lookY - e.position.y) * k;
    }
    const blinkable =
      kind === "pill" ||
      kind === "round" ||
      kind === "square" ||
      kind === "visor" ||
      kind === "dot";
    const open = blinkable ? p.open : 1;
    for (const [eye, s] of [
      [leftEye.current, p.left],
      [rightEye.current, p.right],
    ] as const) {
      if (!eye) continue;
      eye.scale.x += (s - eye.scale.x) * k;
      eye.scale.y += (s * open - eye.scale.y) * Math.min(1, dt * 25);
    }
    m.led.emissiveIntensity += (2.4 * p.glow - m.led.emissiveIntensity) * k;
  });

  // An eye sits on the screen surface — curved or flat — facing along its normal.
  const eyeAt = (x: number): { z: number; rotY: number } => {
    if (!layout.radius) return { z: layout.eyeZ, rotY: 0 };
    const r2 = layout.radius ** 2 - x * x - (layout.sphere ? layout.eyeY ** 2 : 0);
    const z = Math.sqrt(Math.max(0.01, r2));
    // Raised off the screen enough that glancing sideways across a curved face
    // (the eyes slide in x without following the curve) never sinks them into it.
    return { z: z + 0.01, rotY: Math.atan2(x, z) };
  };
  const leftAt = eyeAt(-layout.eyeX);
  const rightAt = eyeAt(layout.eyeX);
  const centerAt = eyeAt(0);

  return (
    <group>
      <group ref={root}>
        <group ref={body}>
          {/* Keyed so a shape change remounts the parts rather than letting React reuse
              one shape's meshes for another's: a mesh that had its geometry as a prop
              and is reused for one that declares it as a child ends up with none. */}
          <Body key={appearance.shape} shape={appearance.shape} m={m} />
          <group ref={eyes} position={[0, layout.eyeY, 0]}>
            {single ? (
              <group ref={leftEye} position={[0, 0, centerAt.z]}>
                <Eye key={kind} kind={kind === "line" ? "line" : "visor"} material={m.led} />
              </group>
            ) : (
              <>
                <group
                  ref={leftEye}
                  position={[-layout.eyeX, 0, leftAt.z]}
                  rotation={[0, leftAt.rotY, 0]}
                >
                  <Eye key={kind} kind={kind} material={m.led} />
                </group>
                <group
                  ref={rightEye}
                  position={[layout.eyeX, 0, rightAt.z]}
                  rotation={[0, rightAt.rotY, 0]}
                >
                  <Eye key={kind} kind={kind} material={m.led} />
                </group>
              </>
            )}
          </group>
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
