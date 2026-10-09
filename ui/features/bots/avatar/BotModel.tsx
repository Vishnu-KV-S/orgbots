"use client";

import { useFrame } from "@react-three/fiber";
import { useEffect, useMemo, useRef } from "react";
import * as THREE from "three";
import type { Appearance, Shape } from "./appearance";
import { applyFace, cloneFace, easeFace, glowMaterial, screenMaterial, targetFace } from "./eyes";
import {
  capsule,
  catEar,
  earPod,
  podShell,
  roundedBox,
  type Surface,
  slab,
  sphere,
  torus,
} from "./geometry";
import type { Mood } from "./mood";
import { textures } from "./textures";

/**
 * One bot, finished like a glossy consumer product: a clear-coated white shell,
 * polished chrome, candy-gloss accents in the bot's colour, and a glass visor.
 *
 * **Every bot shares the finish, the eyes and the ears**; what differs is
 * the shape, the top piece and the accent colour. That is the look of a product
 * family rather than a toy box.
 *
 * **Everything is smooth.** Shells are subdivided rounded boxes or turned profiles,
 * the visor is a pillowed slab bent round the body, and the hardware is domes and
 * capsules — a glossy finish shows every facet, so there are none to show.
 *
 * **The face is a display**: a deep black panel under a clear glass cover, with soft
 * LED eyes drawn into it (`eyes.ts`). The glass is drawn additively, so it adds only
 * its reflections and the eyes shine through untouched. The eyes are the only light.
 *
 * The body animation is a pose function — mood and time in, target transform out —
 * damped every frame, so moods blend instead of snapping.
 */

/** Every bot's shell is pure white; the accent colour is what tells them apart. */
const SHELL = "#ffffff";

interface Layout {
  /** The display: its centre, its size, and what it is bent round. */
  face: { y: number; z: number; w: number; h: number; r: number; surface: Surface };
  eyeX: number;
  /** The eyes' height relative to the display's centre. */
  eyeY: number;
  topY: number;
  floorY: number;
  /** A side ear: where it sits on the right side (the left mirrors it), the tilt of the
   * shell's normal there, and how far it is turned toward the front. */
  ear: { x: number; y: number; z: number; tilt: number; yaw: number };
  /** Where a pair of top pieces (cat ears, knobs) sits, and the shell's tilt there. */
  crown: { x: number; y: number; tilt: number };
}

const LAYOUT: Record<Shape, Layout> = {
  orb: {
    face: { y: 0, z: 0.6, w: 0.84, h: 0.54, r: 0.22, surface: { kind: "sphere", radius: 0.6 } },
    eyeX: 0.15,
    eyeY: 0.02,
    topY: 0.6,
    floorY: -0.62,
    ear: { x: 0.5725, y: 0.04, z: 0.175, tilt: 0.067, yaw: 0.3 },
    crown: { x: 0.25, y: 0.5454, tilt: 0.43 },
  },
  cube: {
    face: { y: -0.02, z: 0.5, w: 0.62, h: 0.44, r: 0.16, surface: { kind: "sphere", radius: 2.2 } },
    eyeX: 0.15,
    eyeY: 0,
    topY: 0.475,
    floorY: -0.5,
    ear: { x: 0.55, y: 0.06, z: 0.06, tilt: 0, yaw: 0.22 },
    crown: { x: 0.25, y: 0.475, tilt: 0 },
  },
  capsule: {
    face: { y: 0.13, z: 0.42, w: 0.74, h: 0.34, r: 0.16, surface: { kind: "cylinder", radius: 0.42 } },
    eyeX: 0.12,
    eyeY: 0,
    topY: 0.67,
    floorY: -0.69,
    ear: { x: 0.4016, y: 0.13, z: 0.1228, tilt: 0, yaw: 0.3 },
    crown: { x: 0.22, y: 0.6077, tilt: 0.55 },
  },
  pod: {
    face: {
      y: -0.12,
      z: 0.5276,
      w: 0.92,
      h: 0.27,
      r: 0.12,
      surface: { kind: "cylinder", radius: 0.5276, taper: 0.1316 },
    },
    eyeX: 0.15,
    eyeY: 0,
    topY: 0.6,
    floorY: -0.42,
    ear: { x: 0.5203, y: 0.13, z: 0.159, tilt: 0.146, yaw: 0.3 },
    crown: { x: 0.25, y: 0.5399, tilt: 0.47 },
  },
  tv: {
    face: { y: 0, z: 0.4, w: 0.72, h: 0.46, r: 0.14, surface: { kind: "sphere", radius: 1.5 } },
    eyeX: 0.16,
    eyeY: 0,
    topY: 0.46,
    floorY: -0.62,
    ear: { x: 0.6, y: 0.06, z: 0.04, tilt: 0, yaw: 0.22 },
    crown: { x: 0.28, y: 0.46, tilt: 0 },
  },
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

/** A smooth 0→1→0 bump over the first `width` of a cycle. */
const bump = (c: number, width: number) =>
  c >= 0 && c < width ? Math.sin((c / width) * Math.PI) : 0;

/** Movements are small and weighted — a heavy object hovering, not a balloon. */
function pose(mood: Mood, t: number): Pose {
  const p: Pose = { ...BASE };
  switch (mood) {
    case "idle":
      p.y = 0.025 * Math.sin(t * 1.4);
      p.rotZ = 0.02 * Math.sin(t * 0.8);
      p.rotY = 0.16 * Math.sin(t * 0.3);
      p.sy = 1 + 0.006 * Math.sin(t * 1.4 + 1);
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
      const hit = bump((t % 0.8) / 0.8, 0.2);
      p.sy = 1 - 0.06 * hit;
      p.sx = 1 + 0.03 * hit;
      p.y = -0.03 * hit;
      p.rotX = 0.07 * hit;
      break;
    }
    case "typing":
      p.x = 0.003 * Math.sin(t * 55);
      p.rotZ = 0.008 * Math.sin(t * 41);
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
      p.y = 0.12 * bump(c, 0.4);
      const land = bump(c - 0.4, 0.1);
      p.sy = 1 - 0.05 * land;
      p.sx = 1 + 0.03 * land;
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

/** The finishes every bot shares, made once. */
let shared: ReturnType<typeof sharedMaterials> | null = null;
function sharedMaterials() {
  return {
    // Gloss white: a satin base under a crisp clear coat, like painted ABS. The
    // clear coat carries the sharp softbox reflections; the base keeps the white soft.
    shell: new THREE.MeshPhysicalMaterial({
      color: SHELL,
      roughness: 0.3,
      specularIntensity: 0.4,
      clearcoat: 1,
      clearcoatRoughness: 0.035,
    }),
    // Piano black, for the ear caps and the antenna's foot.
    gloss: new THREE.MeshPhysicalMaterial({
      color: "#121317",
      roughness: 0.22,
      clearcoat: 1,
      clearcoatRoughness: 0.03,
    }),
    // Polished chrome. It mirrors the studio, so it reads as metal on any background.
    metal: new THREE.MeshPhysicalMaterial({
      color: "#e9ebef",
      metalness: 1,
      roughness: 0.09,
    }),
    // Clear cover glass, drawn additively: black adds nothing, so all that shows is
    // its reflections, laid over the display beneath.
    glass: new THREE.MeshPhysicalMaterial({
      color: "#000000",
      roughness: 0.04,
      clearcoat: 1,
      clearcoatRoughness: 0.02,
      envMapIntensity: 1.5,
      transparent: true,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
    }),
    // The hairline gap round the display and along the shell's part lines.
    seam: new THREE.MeshStandardMaterial({ color: "#0c0c0e", roughness: 0.6 }),
  };
}

function useMaterials(a: Appearance, layout: Layout) {
  shared ??= sharedMaterials();
  const own = useMemo(() => {
    // The display itself: soft LED eyes, drawn by a shader (`eyes.ts`).
    const screen = screenMaterial(a, [layout.face.w / 2, layout.face.h / 2]);
    return {
      // Candy gloss in the bot's colour: the ear rings, the top piece's details.
      accent: new THREE.MeshPhysicalMaterial({
        color: a.glow,
        roughness: 0.28,
        clearcoat: 1,
        clearcoatRoughness: 0.04,
      }),
      screen,
      // The display's light, spilling onto the shell round it.
      glow: glowMaterial(screen, layout.face.r),
    };
  }, [a.glow, layout]);
  useEffect(() => () => Object.values(own).forEach((m) => m.dispose()), [own]);
  return { ...shared, ...own };
}

type Mats = ReturnType<typeof useMaterials>;

/** The display: a deep black panel with a rounded, pillowed edge, a hairline seam
 * round it, and clear glass over it, all bent to the body — and, under it all, the
 * light the screen spills onto the shell when it shines. */
function Visor({ m, layout }: { m: Mats; layout: Layout }) {
  const { y, z, w, h, r, surface } = layout.face;
  // A domed face bulges out of a flat one: lift it so its corners sit on the shell.
  let lift = 0;
  if (surface.kind === "sphere" && surface.radius > 1) {
    const R = surface.radius;
    const cx = w / 2 - r * 0.3;
    const cy = h / 2 - r * 0.3;
    lift = R - R * Math.cos(cx / R) * Math.cos(cy / R) - 0.004;
  }
  const sink = lift + 0.04;
  // The light the display casts lies on the shell itself, so it follows the shell:
  // round the sphere or cylinder, or flat on a box's face.
  const shell: Surface = surface.kind === "sphere" && surface.radius > 1 ? { kind: "flat" } : surface;
  return (
    <>
      <mesh
        geometry={slab(w + 0.24, h + 0.24, r + 0.12, 0.005, 0.002, shell, 0.04)}
        material={m.glow}
        position={[0, y, z]}
        renderOrder={1}
      />
      <group position={[0, y, z + lift]}>
        <mesh geometry={slab(w + 0.026, h + 0.026, r + 0.013, 0.004, 0.003, surface, sink)} material={m.seam} />
        <mesh geometry={slab(w, h, r, 0.026, 0.02, surface, sink)} material={m.screen} />
        <mesh
          geometry={slab(w + 0.003, h + 0.003, r + 0.0015, 0.0275, 0.0215, surface, sink)}
          material={m.glass}
          renderOrder={2}
        />
      </group>
    </>
  );
}

function Body({ shape, m }: { shape: Shape; m: Mats }) {
  switch (shape) {
    case "orb":
      return (
        <group>
          <mesh geometry={sphere(0.6, 160, 120)} material={m.shell} />
          <mesh geometry={torus(0.48, 0.0028)} material={m.seam} rotation={[Math.PI / 2, 0, 0]} position={[0, 0.36, 0]} />
        </group>
      );
    case "cube":
      return <mesh geometry={roundedBox(1.1, 0.95, 1.0, 0.22)} material={m.shell} />;
    case "capsule":
      return (
        <group>
          <mesh geometry={capsule(0.42, 0.5, 128)} material={m.shell} />
          <mesh geometry={torus(0.42, 0.0028)} material={m.seam} rotation={[Math.PI / 2, 0, 0]} position={[0, -0.18, 0]} />
        </group>
      );
    case "pod":
      return (
        <group>
          <mesh geometry={podShell()} material={m.shell} />
          <mesh geometry={torus(0.551, 0.0028)} material={m.seam} rotation={[Math.PI / 2, 0, 0]} position={[0, 0.05, 0]} />
        </group>
      );
    case "tv":
      return (
        <group>
          <mesh geometry={roundedBox(1.2, 0.92, 0.8, 0.2)} material={m.shell} />
          {[-1, 1].map((s) => (
            <mesh
              key={s}
              geometry={capsule(0.03, 0.14, 32)}
              material={m.metal}
              position={[s * 0.34, -0.53, 0]}
              rotation={[0, 0, s * 0.22]}
            />
          ))}
        </group>
      );
  }
}

/** Round ear pods on both sides, like a pair of headphones: a white shell, a ring in
 * the bot's colour, a piano-black cap. */
function Ears({ m, layout }: { m: Mats; layout: Layout }) {
  const { x, y, z, tilt, yaw } = layout.ear;
  return (
    <>
      {[-1, 1].map((s) => (
        <group key={s} position={[s * x, y, z]} rotation={[0, -s * yaw, 0]}>
          <group rotation={[0, 0, -s * (Math.PI / 2 - tilt)]}>
            <mesh geometry={earPod()} material={m.shell} />
            <mesh geometry={torus(0.098, 0.015)} material={m.accent} position={[0, 0.064, 0]} rotation={[Math.PI / 2, 0, 0]} />
            <mesh geometry={sphere(0.068)} material={m.gloss} position={[0, 0.066, 0]} scale={[1, 0.38, 1]} />
          </group>
        </group>
      ))}
    </>
  );
}

function TopPiece({ a, m, layout, phase }: { a: Appearance; m: Mats; layout: Layout; phase: number }) {
  const wobble = useRef<THREE.Group>(null);
  const twitch = useRef<THREE.Group>(null);
  useFrame((state) => {
    const t = state.clock.elapsedTime + phase;
    if (wobble.current) wobble.current.rotation.z = 0.05 * Math.sin(t * 2.6);
    // Now and then one cat ear flicks.
    if (twitch.current) twitch.current.rotation.x = -0.35 * bump(t % 5.3, 0.22);
  });
  const y = layout.topY;
  const crown = layout.crown;
  switch (a.top) {
    case "ring":
      // A lens-like button: a chrome bezel, the accent ring, a black glass centre.
      return (
        <group position={[0, y - 0.02, 0]}>
          <mesh geometry={torus(0.125, 0.022)} material={m.metal} rotation={[Math.PI / 2, 0, 0]} />
          <mesh geometry={torus(0.094, 0.012)} material={m.accent} rotation={[Math.PI / 2, 0, 0]} position={[0, 0.008, 0]} />
          <mesh geometry={sphere(0.085)} material={m.gloss} position={[0, 0.004, 0]} scale={[1, 0.3, 1]} />
        </group>
      );
    case "knobs":
      return (
        <>
          {[-1, 1].map((s) => (
            <group key={s} position={[s * crown.x, crown.y, 0]} rotation={[0, 0, -s * crown.tilt]}>
              <mesh geometry={capsule(0.058, 0.07)} material={m.metal} position={[0, 0.04, 0]} />
              <mesh geometry={sphere(0.042)} material={m.accent} position={[0, 0.135, 0]} scale={[1, 0.65, 1]} />
            </group>
          ))}
        </>
      );
    case "antenna":
      return (
        <group ref={wobble} position={[0, y - 0.02, 0]}>
          <mesh geometry={sphere(0.06)} material={m.gloss} scale={[1, 0.5, 1]} />
          <mesh geometry={capsule(0.011, 0.26, 24)} material={m.metal} position={[0, 0.15, 0]} />
          <mesh geometry={sphere(0.042)} material={m.accent} position={[0, 0.31, 0]} />
        </group>
      );
    case "ears":
      // Cat ears: white shells, the bot's colour inside.
      return (
        <>
          {[-1, 1].map((s) => (
            <group
              key={s}
              ref={s > 0 ? twitch : undefined}
              position={[s * crown.x, crown.y - 0.01, 0]}
              rotation={[0, 0, -s * (crown.tilt * 0.75 + 0.18)]}
            >
              <mesh geometry={catEar()} material={m.shell} scale={[1, 1, 0.48]} />
              <mesh geometry={catEar()} material={m.accent} scale={[0.6, 0.7, 0.3]} position={[0, 0.02, 0.026]} />
            </group>
          ))}
        </>
      );
    case "halo":
      // A sensor ring on a short mast.
      return (
        <group position={[0, y - 0.02, 0]}>
          <mesh geometry={capsule(0.018, 0.1, 24)} material={m.metal} position={[0, 0.07, 0]} />
          <mesh geometry={torus(0.1, 0.016)} material={m.metal} position={[0, 0.15, 0]} rotation={[Math.PI / 2, 0, 0]} />
          <mesh geometry={torus(0.1, 0.0065)} material={m.accent} position={[0, 0.15, 0.012]} rotation={[Math.PI / 2, 0, 0]} />
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
  const m = useMaterials(appearance, layout);
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
    applyFace(m.screen, face.current, 0, layout.eyeY, t);
  });

  return (
    <group>
      <group ref={root}>
        <group ref={body}>
          {/* Keyed so a shape change remounts the parts rather than reusing one shape's
              meshes for another's. */}
          <group key={appearance.shape}>
            <Body shape={appearance.shape} m={m} />
            <Visor m={m} layout={layout} />
            <Ears m={m} layout={layout} />
          </group>
          <TopPiece
            key={`${appearance.top}:${appearance.shape}`}
            a={appearance}
            m={m}
            layout={layout}
            phase={phase}
          />
        </group>
      </group>
      {detail === "high" && <ContactShadow y={layout.floorY} />}
    </group>
  );
}
