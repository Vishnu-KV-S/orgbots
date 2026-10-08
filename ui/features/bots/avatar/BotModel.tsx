"use client";

import { RoundedBox, Sparkles } from "@react-three/drei";
import { useFrame } from "@react-three/fiber";
import { useEffect, useMemo, useRef } from "react";
import * as THREE from "three";
import { RoundedBoxGeometry } from "three/examples/jsm/geometries/RoundedBoxGeometry.js";
import type { Appearance, Shape } from "./appearance";
import type { Mood } from "./mood";

/**
 * One bot, built from primitives so that every combination of shape, eyes, top,
 * finish and colours is a valid body, and animated by mood.
 *
 * The animation is a pose function — mood and time in, target transform out — with
 * every channel damped toward its target each frame. So switching from "browsing" to
 * "clicking" mid-motion blends instead of snapping, and each mood is a dozen lines of
 * arithmetic rather than a keyframe file.
 */

interface Layout {
  /** Front surface of a flat visor, where eyes sit. */
  eyeZ: number;
  /** For a curved visor: its radius, and whether it curves vertically too (a sphere)
   * or only around the vertical axis (a cylinder). Eyes are placed on the surface. */
  radius?: number;
  sphere?: boolean;
  eyeY: number;
  eyeX: number;
  /** Eyes on a curved face turn outward by this much per unit of x. */
  curve: number;
  topY: number;
  halfWidth: number;
  floorY: number;
}

const LAYOUT: Record<Shape, Layout> = {
  orb: {
    eyeZ: 0.6,
    radius: 0.607,
    sphere: true,
    eyeY: 0.02,
    eyeX: 0.17,
    curve: 1,
    topY: 0.58,
    halfWidth: 0.6,
    floorY: -0.78,
  },
  cube: {
    eyeZ: 0.525,
    eyeY: -0.02,
    eyeX: 0.2,
    curve: 0,
    topY: 0.47,
    halfWidth: 0.55,
    floorY: -0.7,
  },
  capsule: {
    eyeZ: 0.44,
    radius: 0.426,
    eyeY: 0.13,
    eyeX: 0.14,
    curve: 1,
    topY: 0.66,
    halfWidth: 0.42,
    floorY: -0.82,
  },
  pod: {
    eyeZ: 0.56,
    radius: 0.548,
    eyeY: -0.12,
    eyeX: 0.18,
    curve: 1,
    topY: 0.6,
    halfWidth: 0.56,
    floorY: -0.7,
  },
  tv: {
    eyeZ: 0.43,
    eyeY: 0.0,
    eyeX: 0.2,
    curve: 0,
    topY: 0.46,
    halfWidth: 0.6,
    floorY: -0.8,
  },
};

const ERROR_GLOW = new THREE.Color("#ff3b3b");

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
  /** Multiplies the glow. */
  glow: number;
  /** Per-eye scale, for the lopsided "huh?" look. */
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

const blinkAt = (t: number, every: number) => (t % every < 0.12 ? 0.08 : 1);

function pose(mood: Mood, t: number): Pose {
  const p: Pose = { ...BASE };
  switch (mood) {
    case "idle":
      p.y = 0.05 * Math.sin(t * 1.6);
      p.rotZ = 0.04 * Math.sin(t * 0.9);
      p.rotY = 0.22 * Math.sin(t * 0.35);
      p.lookX = 0.035 * Math.tanh(4 * Math.sin(t * 0.45));
      p.open = blinkAt(t, 3.7);
      break;
    case "thinking":
      p.y = 0.04 * Math.sin(t * 2);
      p.rotZ = 0.18 * Math.sin(t * 0.8);
      p.lookX = 0.03 + 0.01 * Math.sin(t * 1.3);
      p.lookY = 0.035;
      p.glow = 1 + 0.35 * Math.sin(t * 3);
      p.open = blinkAt(t, 2.9);
      break;
    case "browsing":
      p.y = 0.03 * Math.sin(t * 2.5);
      p.lookX = 0.055 * Math.sin(t * 2.2);
      p.rotY = 0.3 * Math.sin(t * 2.2);
      p.open = blinkAt(t, 2.2);
      break;
    case "clicking": {
      const c = (t % 0.75) / 0.75;
      const hit = c < 0.16;
      p.sy = hit ? 0.84 : 1 + 0.04 * Math.sin(c * Math.PI);
      p.sx = hit ? 1.12 : 1;
      p.y = hit ? -0.06 : 0.04 * Math.sin(c * Math.PI);
      p.rotX = hit ? 0.12 : 0;
      p.open = hit ? 0.45 : 1;
      p.lookY = -0.02;
      break;
    }
    case "typing":
      p.x = 0.01 * Math.sin(t * 61);
      p.rotZ = 0.025 * Math.sin(t * 47);
      p.y = 0.015 * Math.sin(t * 9);
      p.lookY = -0.035;
      p.open = 0.55;
      break;
    case "waiting":
      p.rotZ = 0.22 + 0.05 * Math.sin(t * 3);
      p.rotY = 0.14 * Math.sin(t * 4);
      p.y = 0.03 * Math.sin(t * 2);
      p.left = 1.3;
      p.right = 0.8;
      p.glow = 1 + 0.4 * Math.max(0, Math.sin(t * 4));
      p.lookY = 0.02;
      break;
    case "error": {
      const env = Math.max(0, 1 - (t % 1.6) / 0.6);
      p.x = 0.07 * Math.sin(t * 42) * env;
      p.rotZ = -0.08;
      p.y = -0.02;
      p.glow = 1.2;
      break;
    }
    case "happy": {
      const c = (t % 1.3) / 1.3;
      p.y = c < 0.5 ? 0.28 * Math.sin(c * 2 * Math.PI) : 0;
      p.sy = c > 0.5 && c < 0.6 ? 0.88 : 1;
      p.sx = c > 0.5 && c < 0.6 ? 1.08 : 1;
      p.spin = t % 2.6 < 0.65;
      p.glow = 1.3;
      break;
    }
    case "creating":
      p.y = 0.1 + 0.05 * Math.sin(t * 4);
      p.spin = true;
      p.left = 1.2;
      p.right = 1.2;
      p.glow = 1.7;
      break;
    case "delegating":
      p.rotX = 0.16 + 0.07 * Math.sin(t * 5);
      p.rotY = 0.32;
      p.y = 0.02 * Math.sin(t * 5);
      p.lookX = 0.04;
      p.open = blinkAt(t, 1.4);
      p.glow = 1.2;
      break;
    case "remembering":
      p.rotX = 0.1 * Math.sin(t * 2);
      p.y = 0.03 * Math.sin(t * 2);
      p.glow = 1.6 + 0.5 * Math.sin(t * 3);
      break;
    case "sleeping":
      p.y = -0.05 + 0.025 * Math.sin(t * 1.1);
      p.sy = 1 + 0.02 * Math.sin(t * 1.1);
      p.rotZ = 0.12;
      p.glow = 0.35;
      break;
    case "stopped":
      p.y = -0.09;
      p.rotX = 0.28;
      p.glow = 0.15;
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
  const materials = useMemo(() => {
    const body = new THREE.MeshPhysicalMaterial({ color: a.body });
    switch (a.finish) {
      case "gloss":
        Object.assign(body, {
          roughness: 0.22,
          metalness: 0,
          clearcoat: 1,
          clearcoatRoughness: 0.08,
        });
        break;
      case "matte":
        Object.assign(body, { roughness: 0.75, metalness: 0, clearcoat: 0.1 });
        break;
      case "metal":
        Object.assign(body, {
          roughness: 0.28,
          metalness: 0.85,
          clearcoat: 0.5,
        });
        break;
      case "pearl":
        Object.assign(body, {
          roughness: 0.3,
          metalness: 0.05,
          clearcoat: 1,
          clearcoatRoughness: 0.12,
          iridescence: 0.6,
          iridescenceIOR: 1.35,
          sheen: 0.6,
          sheenColor: new THREE.Color(a.glow),
        });
        break;
    }
    const visor = new THREE.MeshPhysicalMaterial({
      color: "#07060c",
      roughness: 0.3,
      metalness: 0.25,
      clearcoat: 1,
      clearcoatRoughness: 0.25,
      // A faint wash of the glow colour, so the visor reads as tinted glass lit from
      // inside rather than a hole in the body.
      emissive: new THREE.Color(a.glow).multiplyScalar(0.07),
    });
    const glow = new THREE.MeshBasicMaterial({
      color: a.glow,
      toneMapped: false,
    });
    const metal = new THREE.MeshStandardMaterial({
      color: "#c9cbd6",
      metalness: 0.9,
      roughness: 0.25,
    });
    const trim = new THREE.MeshPhysicalMaterial({
      color: new THREE.Color(a.body).multiplyScalar(0.82),
      roughness: 0.35,
      clearcoat: 0.6,
    });
    return { body, visor, glow, metal, trim };
  }, [a.body, a.glow, a.finish]);

  useEffect(() => () => Object.values(materials).forEach((m) => m.dispose()), [materials]);

  // An error turns the glow red; everything else keeps the bot's own colour.
  useEffect(() => {
    materials.glow.color.set(mood === "error" ? ERROR_GLOW : a.glow);
  }, [materials, mood, a.glow]);

  return materials;
}

const EYE_GEOMETRY = {
  pill: new RoundedBoxGeometry(0.12, 0.2, 0.04, 4, 0.05),
  round: new THREE.CylinderGeometry(0.085, 0.085, 0.03, 32).rotateX(Math.PI / 2),
  square: new RoundedBoxGeometry(0.17, 0.15, 0.04, 4, 0.045),
  visor: new RoundedBoxGeometry(0.5, 0.09, 0.04, 4, 0.04),
  dot: new THREE.SphereGeometry(0.055, 24, 16),
  arc: new THREE.TorusGeometry(0.075, 0.024, 12, 32, Math.PI),
  bar: new THREE.BoxGeometry(0.17, 0.034, 0.03),
  line: new RoundedBoxGeometry(0.15, 0.03, 0.03, 2, 0.012),
};

function Eye({ kind, material }: { kind: EyeKind; material: THREE.Material }) {
  if (kind === "x") {
    return (
      <group>
        <mesh geometry={EYE_GEOMETRY.bar} material={material} rotation={[0, 0, Math.PI / 4]} />
        <mesh geometry={EYE_GEOMETRY.bar} material={material} rotation={[0, 0, -Math.PI / 4]} />
      </group>
    );
  }
  const geometry =
    kind === "arc" ? EYE_GEOMETRY.arc : kind === "line" ? EYE_GEOMETRY.line : EYE_GEOMETRY[kind];
  return <mesh geometry={geometry} material={material} />;
}

function Body({ shape, m }: { shape: Shape; m: ReturnType<typeof useMaterials> }) {
  switch (shape) {
    case "orb":
      return (
        <group>
          <mesh material={m.body}>
            <sphereGeometry args={[0.6, 64, 48]} />
          </mesh>
          {/* The visor: a patch of a slightly larger sphere across the front. */}
          <mesh material={m.visor}>
            <sphereGeometry args={[0.607, 64, 48, Math.PI / 2 - 0.82, 1.64, 1.02, 0.98]} />
          </mesh>
          <mesh material={m.trim} rotation={[Math.PI / 2, 0, 0]} position={[0, 0.31, 0]}>
            <torusGeometry args={[0.515, 0.012, 8, 64]} />
          </mesh>
          {/* Side vents, like the reference orb's cheek panels. */}
          {[-1, 1].map((s) => (
            <RoundedBox
              key={s}
              args={[0.05, 0.16, 0.24]}
              radius={0.02}
              position={[s * 0.585, 0.08, 0.08]}
              rotation={[0, s * 0.2, 0]}
              material={m.trim}
            />
          ))}
        </group>
      );
    case "cube":
      return (
        <group>
          <RoundedBox args={[1.1, 0.95, 1.0]} radius={0.2} smoothness={6} material={m.body} />
          <RoundedBox
            args={[0.9, 0.66, 0.06]}
            radius={0.14}
            smoothness={5}
            position={[0, -0.02, 0.495]}
            material={m.visor}
          />
          {[
            [-0.43, 0.45, 0.38],
            [0.43, 0.45, 0.38],
            [-0.43, 0.45, -0.38],
            [0.43, 0.45, -0.38],
            [-0.53, -0.3, 0.3],
            [0.53, -0.3, 0.3],
          ].map((pos) => (
            <mesh key={pos.join()} position={pos as [number, number, number]} material={m.metal}>
              <sphereGeometry args={[0.028, 12, 8]} />
            </mesh>
          ))}
        </group>
      );
    case "capsule":
      return (
        <group>
          <mesh material={m.body}>
            <capsuleGeometry args={[0.42, 0.5, 16, 40]} />
          </mesh>
          <mesh material={m.visor} position={[0, 0.13, 0]}>
            <cylinderGeometry args={[0.426, 0.426, 0.34, 48, 1, true, -0.95, 1.9]} />
          </mesh>
          <mesh material={m.trim} rotation={[Math.PI / 2, 0, 0]} position={[0, -0.18, 0]}>
            <torusGeometry args={[0.422, 0.012, 8, 64]} />
          </mesh>
        </group>
      );
    case "pod":
      return (
        <group>
          <mesh material={m.body} position={[0, 0.05, 0]}>
            <sphereGeometry args={[0.55, 56, 32, 0, Math.PI * 2, 0, Math.PI / 2]} />
          </mesh>
          <mesh material={m.body} position={[0, -0.2, 0]}>
            <cylinderGeometry args={[0.55, 0.48, 0.5, 56]} />
          </mesh>
          <mesh material={m.visor} position={[0, -0.12, 0]}>
            <cylinderGeometry args={[0.548, 0.53, 0.26, 56, 1, true, -1, 2]} />
          </mesh>
          <mesh material={m.trim} rotation={[Math.PI / 2, 0, 0]} position={[0, 0.05, 0]}>
            <torusGeometry args={[0.55, 0.014, 8, 64]} />
          </mesh>
        </group>
      );
    case "tv":
      return (
        <group>
          <RoundedBox args={[1.2, 0.92, 0.8]} radius={0.14} smoothness={5} material={m.body} />
          <RoundedBox
            args={[0.92, 0.68, 0.06]}
            radius={0.12}
            smoothness={5}
            position={[0, 0, 0.395]}
            material={m.visor}
          />
          {[-1, 1].map((s) => (
            <mesh
              key={s}
              material={m.metal}
              position={[s * 0.36, -0.54, 0]}
              rotation={[0, 0, s * 0.25]}
            >
              <cylinderGeometry args={[0.035, 0.025, 0.2, 12]} />
            </mesh>
          ))}
        </group>
      );
  }
}

function TopPiece({
  a,
  m,
  layout,
}: {
  a: Appearance;
  m: ReturnType<typeof useMaterials>;
  layout: Layout;
}) {
  const wobble = useRef<THREE.Group>(null);
  const halo = useRef<THREE.Mesh>(null);
  useFrame((state) => {
    const t = state.clock.elapsedTime;
    if (wobble.current) wobble.current.rotation.z = 0.12 * Math.sin(t * 3.1);
    if (halo.current) halo.current.rotation.z = t * 0.8;
  });
  const y = layout.topY;
  switch (a.top) {
    case "ring":
      return (
        <group position={[0, y, 0]}>
          <mesh material={m.trim} rotation={[Math.PI / 2, 0, 0]}>
            <torusGeometry args={[0.15, 0.04, 16, 48]} />
          </mesh>
          <mesh material={m.visor} position={[0, -0.005, 0]}>
            <cylinderGeometry args={[0.12, 0.12, 0.04, 40]} />
          </mesh>
        </group>
      );
    case "knobs":
      return (
        <group position={[0, y, 0]}>
          {[-1, 1].map((s) => (
            <group key={s} position={[s * 0.26, 0.05, 0]}>
              <mesh material={m.metal}>
                <cylinderGeometry args={[0.07, 0.095, 0.12, 24]} />
              </mesh>
              <mesh material={m.metal} position={[0, 0.1, 0]}>
                <cylinderGeometry args={[0.045, 0.045, 0.1, 20]} />
              </mesh>
            </group>
          ))}
        </group>
      );
    case "antenna":
      return (
        <group ref={wobble} position={[0, y - 0.02, 0]}>
          <mesh material={m.metal} position={[0, 0.14, 0]}>
            <cylinderGeometry args={[0.014, 0.018, 0.28, 10]} />
          </mesh>
          <mesh material={m.glow} position={[0, 0.3, 0]}>
            <sphereGeometry args={[0.055, 20, 14]} />
          </mesh>
        </group>
      );
    case "ears":
      return (
        <group>
          {[-1, 1].map((s) => (
            <RoundedBox
              key={s}
              args={[0.1, 0.3, 0.24]}
              radius={0.04}
              position={[s * (layout.halfWidth + 0.03), 0.05, 0]}
              material={m.trim}
            />
          ))}
        </group>
      );
    case "halo":
      return (
        <mesh
          ref={halo}
          material={m.glow}
          position={[0, y + 0.2, 0]}
          rotation={[Math.PI / 2, 0, 0]}
        >
          <torusGeometry args={[0.24, 0.018, 12, 64]} />
        </mesh>
      );
    case "none":
      return null;
  }
}

/** Three dots orbiting overhead — "thinking". */
function ThoughtDots({ m, y }: { m: ReturnType<typeof useMaterials>; y: number }) {
  const g = useRef<THREE.Group>(null);
  useFrame((state) => {
    if (!g.current) return;
    const t = state.clock.elapsedTime;
    g.current.rotation.y = t * 2.2;
    g.current.children.forEach((c, i) => {
      c.position.y = 0.05 * Math.sin(t * 4 + i * 2);
    });
  });
  return (
    <group ref={g} position={[0, y + 0.2, 0]}>
      {[0, 1, 2].map((i) => (
        <mesh
          key={i}
          material={m.glow}
          position={[
            0.3 * Math.cos((i * 2 * Math.PI) / 3),
            0,
            0.3 * Math.sin((i * 2 * Math.PI) / 3),
          ]}
        >
          <sphereGeometry args={[0.045, 14, 10]} />
        </mesh>
      ))}
    </group>
  );
}

/** A floating "?" built from a torus arc, a stem and a dot — no font to load. */
function QuestionMark({ m, y, x }: { m: ReturnType<typeof useMaterials>; y: number; x: number }) {
  const g = useRef<THREE.Group>(null);
  useFrame((state) => {
    if (!g.current) return;
    const t = state.clock.elapsedTime;
    g.current.position.y = y + 0.18 + 0.04 * Math.sin(t * 3);
    g.current.rotation.y = 0.5 * Math.sin(t * 1.5);
  });
  return (
    <group ref={g} position={[x + 0.3, y + 0.18, 0.1]} scale={0.85}>
      <mesh material={m.glow} rotation={[0, 0, -Math.PI / 2]} position={[0, 0.1, 0]}>
        <torusGeometry args={[0.08, 0.026, 10, 24, Math.PI * 1.35]} />
      </mesh>
      <mesh material={m.glow} position={[0, -0.02, 0]}>
        <cylinderGeometry args={[0.026, 0.026, 0.09, 10]} />
      </mesh>
      <mesh material={m.glow} position={[0, -0.13, 0]}>
        <sphereGeometry args={[0.032, 12, 8]} />
      </mesh>
    </group>
  );
}

/** Rings expanding from the top — "asking a helper". */
function Signals({ color, y }: { color: string; y: number }) {
  const rings = useRef<THREE.Mesh[]>([]);
  const mats = useMemo(
    () =>
      [0, 1, 2].map(
        () =>
          new THREE.MeshBasicMaterial({
            color,
            transparent: true,
            toneMapped: false,
          }),
      ),
    [color],
  );
  useEffect(() => () => mats.forEach((m) => m.dispose()), [mats]);
  useFrame((state) => {
    const t = state.clock.elapsedTime;
    rings.current.forEach((ring, i) => {
      if (!ring) return;
      const c = (t * 0.9 + i / 3) % 1;
      ring.scale.setScalar(0.4 + c * 1.6);
      mats[i].opacity = 0.8 * (1 - c);
    });
  });
  return (
    <group position={[0.15, y + 0.15, 0]} rotation={[0, 0, -0.5]}>
      {mats.map((mat, i) => (
        <mesh
          key={i}
          ref={(el) => {
            if (el) rings.current[i] = el;
          }}
          material={mat}
          rotation={[Math.PI / 2, 0, 0]}
        >
          <torusGeometry args={[0.18, 0.012, 8, 40]} />
        </mesh>
      ))}
    </group>
  );
}

function Floor({ color, y }: { color: string; y: number }) {
  return (
    <group position={[0, y, 0]}>
      <mesh rotation={[-Math.PI / 2, 0, 0]}>
        <circleGeometry args={[0.55, 48]} />
        <meshBasicMaterial color="#000000" transparent opacity={0.22} />
      </mesh>
      <mesh rotation={[-Math.PI / 2, 0, 0]} position={[0, 0.002, 0]}>
        <ringGeometry args={[0.32, 0.36, 48]} />
        <meshBasicMaterial color={color} transparent opacity={0.75} toneMapped={false} />
      </mesh>
    </group>
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
  /** `low` for list-sized avatars: no floor, no particles, no extras. */
  detail?: "high" | "low";
  /** Offsets this bot's clock so a list of bots does not bob in lockstep. */
  phase?: number;
}) {
  const layout = LAYOUT[appearance.shape];
  const m = useMaterials(appearance, mood);
  const root = useRef<THREE.Group>(null);
  const body = useRef<THREE.Group>(null);
  const eyes = useRef<THREE.Group>(null);
  const leftEye = useRef<THREE.Group>(null);
  const rightEye = useRef<THREE.Group>(null);
  const light = useRef<THREE.PointLight>(null);
  const spin = useRef(0);
  const sway = useRef(0);
  const kind = eyeKind(appearance.eyes, mood);
  const single = kind === "visor" || (appearance.eyes === "visor" && kind === "line");

  useFrame((state, dt) => {
    const t = state.clock.elapsedTime + phase;
    const p = pose(mood, t);
    const k = Math.min(1, dt * 10);
    const r = root.current;
    const b = body.current;
    if (!r || !b) return;

    r.position.y += (p.y - r.position.y) * k;
    r.position.x += (p.x - r.position.x) * Math.min(1, dt * 30);
    r.rotation.x += (p.rotX - r.rotation.x) * k;
    r.rotation.z += (p.rotZ - r.rotation.z) * k;

    // Spins accumulate; when the mood stops spinning, settle on the nearest whole turn.
    if (p.spin) {
      spin.current += dt * (mood === "creating" ? 7 : 11);
    } else {
      const target = Math.round(spin.current / (Math.PI * 2)) * Math.PI * 2;
      spin.current += (target - spin.current) * Math.min(1, dt * 6);
    }
    sway.current += (p.rotY - sway.current) * k;
    r.rotation.y = spin.current + sway.current;

    b.scale.x += (p.sx - b.scale.x) * Math.min(1, dt * 18);
    b.scale.z = b.scale.x;
    b.scale.y += (p.sy - b.scale.y) * Math.min(1, dt * 18);

    const e = eyes.current;
    if (e) {
      e.position.x += (p.lookX - e.position.x) * k;
      e.position.y += (layout.eyeY + p.lookY - e.position.y) * k;
    }
    const open =
      kind === "pill" || kind === "round" || kind === "square" || kind === "visor" || kind === "dot"
        ? p.open
        : 1;
    for (const [eye, s] of [
      [leftEye.current, p.left],
      [rightEye.current, p.right],
    ] as const) {
      if (!eye) continue;
      eye.scale.x += (s - eye.scale.x) * k;
      eye.scale.y += (s * open - eye.scale.y) * Math.min(1, dt * 25);
    }
    if (light.current) light.current.intensity = 1.6 * p.glow;
    m.visor.emissiveIntensity = 0.5 + 0.5 * p.glow;
  });

  // Where an eye sits: on a curved visor, on its surface (and turned to face along
  // its normal); on a flat one, just in front of it.
  const eyeAt = (x: number): { z: number; rotY: number } => {
    if (!layout.radius) return { z: layout.eyeZ, rotY: 0 };
    const r2 = layout.radius ** 2 - x * x - (layout.sphere ? layout.eyeY ** 2 : 0);
    const z = Math.sqrt(Math.max(0.01, r2));
    return { z: z + 0.012, rotY: Math.atan2(x, z) * layout.curve };
  };
  const leftAt = eyeAt(-layout.eyeX);
  const rightAt = eyeAt(layout.eyeX);
  const centerAt = eyeAt(0);

  return (
    <group>
      <group ref={root}>
        <group ref={body}>
          <Body shape={appearance.shape} m={m} />
          <group ref={eyes} position={[0, layout.eyeY, 0]}>
            {single ? (
              <group ref={leftEye} position={[0, 0, centerAt.z]}>
                <Eye kind={kind === "line" ? "line" : "visor"} material={m.glow} />
              </group>
            ) : (
              <>
                <group
                  ref={leftEye}
                  position={[-layout.eyeX, 0, leftAt.z]}
                  rotation={[0, leftAt.rotY, 0]}
                >
                  <Eye kind={kind} material={m.glow} />
                </group>
                <group
                  ref={rightEye}
                  position={[layout.eyeX, 0, rightAt.z]}
                  rotation={[0, rightAt.rotY, 0]}
                >
                  <Eye kind={kind} material={m.glow} />
                </group>
              </>
            )}
          </group>
          <TopPiece a={appearance} m={m} layout={layout} />
        </group>
        <pointLight
          ref={light}
          position={[0, layout.eyeY, layout.eyeZ + 0.35]}
          color={mood === "error" ? "#ff3b3b" : appearance.glow}
          intensity={1.6}
          distance={2.2}
        />
        {detail === "high" && mood === "thinking" && <ThoughtDots m={m} y={layout.topY} />}
        {detail === "high" && mood === "waiting" && (
          <QuestionMark m={m} y={layout.topY} x={layout.halfWidth * 0.4} />
        )}
        {detail === "high" && mood === "delegating" && (
          <Signals color={appearance.glow} y={layout.topY} />
        )}
      </group>
      {detail === "high" && (mood === "happy" || mood === "creating") && (
        <Sparkles count={28} scale={[1.8, 1.6, 1.4]} size={4} speed={0.8} color={appearance.glow} />
      )}
      {detail === "high" && <Floor color={appearance.glow} y={layout.floorY} />}
    </group>
  );
}
