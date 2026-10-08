import * as THREE from "three";
import type { Appearance } from "./appearance";
import type { Mood } from "./mood";

/**
 * The display: LED dot-matrix eyes, drawn by a shader on the bot's black screen.
 *
 * Each eye is a signed-distance shape — a rounded box with a top lid, a bottom lid
 * and a tilt — and the screen is a grid of round LEDs sampled against it. An LED
 * inside an eye is lit, brighter and larger toward the core, with the hottest dots
 * running toward white; LEDs just outside it glow dimly, which is the halo a real
 * matrix shows through its diffuser. So an expression is a handful of numbers —
 * lids, size, tilt, where the eyes look — and every one of them is eased toward its
 * target each frame, which is what makes a blink close like a lid and a smile curve
 * up instead of switching sprites.
 *
 * At avatar size the dots are finer than a pixel and would shimmer, so the grid fades
 * into a smooth glow as it shrinks (the `fwidth` term below).
 */

export interface EyeShape {
  x: number;
  y: number;
  /** Half width and half height, in body units. */
  w: number;
  h: number;
  /** 0 = square corners, 1 = fully round. */
  round: number;
  /** How far each lid has closed, 0..1 of the eye's height. */
  top: number;
  bottom: number;
  /** Tilt in radians; the outer corner up for positive values on the right eye. */
  tilt: number;
}

export interface Face {
  left: EyeShape;
  right: EyeShape;
  /** 0 normal, 1 X-eyes. */
  cross: number;
  /** Overall LED brightness. */
  bright: number;
  /** 0 = the bot's colour, 1 = error red. */
  alarm: number;
}

const VERT = /* glsl */ `
  varying vec3 vPos;
  void main() {
    vPos = position;
    gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
  }
`;

const FRAG = /* glsl */ `
  uniform vec3 uColor;
  uniform vec3 uAlarm;
  uniform float uAlarmMix;
  uniform vec4 uL; uniform vec4 uLlid;   // x, y, w, h | top, bottom, tilt, round
  uniform vec4 uR; uniform vec4 uRlid;
  uniform float uCross;
  uniform float uBright;
  uniform float uPitch;
  uniform vec2 uOffset;                  // screen-local origin of the eye coordinates
  varying vec3 vPos;

  float eye(vec2 p, vec4 e, vec4 lid) {
    vec2 q = p - e.xy;
    float c = cos(lid.z), s = sin(lid.z);
    q = vec2(c * q.x + s * q.y, -s * q.x + c * q.y);
    float r = lid.w * min(e.z, e.w);
    vec2 d = abs(q) - e.zw + r;
    float box = length(max(d, 0.0)) + min(max(d.x, d.y), 0.0) - r;
    // Lids are curved, like real ones: the top one droops at the sides, the bottom
    // one rises in the middle — which is what turns a closing bottom lid into a smile.
    float topY = e.w - lid.x * 2.0 * e.w;
    float topCut = q.y - topY + 1.4 * q.x * q.x / max(e.z, 0.001) * lid.x;
    // The bottom lid arches *up* in the middle as it rises, so a raised bottom lid
    // leaves the dome of the eye above it: a smile. (Bending it the other way leaves
    // a downward-pointing wedge.)
    float botY = -e.w + lid.y * 2.0 * e.w;
    float xn = clamp(q.x / max(e.z, 0.001), -1.0, 1.0);
    float botCut = botY + 0.9 * lid.y * e.w * (1.0 - xn * xn) - q.y;
    return max(box, max(topCut, botCut));
  }

  float segment(vec2 p, vec2 a, vec2 b) {
    vec2 pa = p - a, ba = b - a;
    float h = clamp(dot(pa, ba) / dot(ba, ba), 0.0, 1.0);
    return length(pa - ba * h);
  }

  float cross2(vec2 p, vec4 e) {
    vec2 q = p - e.xy;
    float k = min(e.z, e.w) * 0.85;
    float d = min(segment(q, vec2(-k, -k), vec2(k, k)), segment(q, vec2(-k, k), vec2(k, -k)));
    return d - k * 0.28;
  }

  float face(vec2 p) {
    float normal = min(eye(p, uL, uLlid), eye(p, uR, uRlid));
    float crossed = min(cross2(p, uL), cross2(p, uR));
    return mix(normal, crossed, uCross);
  }

  // Every smoothstep here is written low-edge-first: GLSL leaves a reversed one
  // undefined, and some GPUs return 0 for it.
  void main() {
    vec2 p = vPos.xy - uOffset;
    vec2 g = p / uPitch;
    vec2 cell = fract(g) - 0.5;
    vec2 center = (floor(g) + 0.5) * uPitch;

    // Each LED takes one value, from its own centre: dots are lit or not, never
    // half-cut by the edge of the shape.
    float d = face(center);
    float core = 1.0 - smoothstep(-0.006, 0.004, d);
    float depth = clamp(-d / 0.03, 0.0, 1.0);
    float halo = exp(-max(d, 0.0) / 0.022) * 0.32 * (1.0 - core);
    float level = max(core * (0.7 + 0.3 * depth), halo);

    // Brighter LEDs read larger, as they do through a diffuser.
    float radius = mix(0.2, 0.44, level);
    float dist = length(cell);
    float aa = fwidth(dist) + 0.02;
    float dotMask = 1.0 - smoothstep(radius - aa, radius + aa, dist);

    // Finer than a couple of pixels, the grid would shimmer: fade to a smooth glow.
    float px = fwidth(g.x);
    float solid = smoothstep(0.42, 0.7, px);
    float smoothLevel = max(1.0 - smoothstep(-0.004, 0.004, face(p)), exp(-max(face(p), 0.0) / 0.02) * 0.3);
    float mask = mix(dotMask * level, smoothLevel, solid);

    vec3 tint = mix(uColor, uAlarm, uAlarmMix);
    // The core runs toward white the way an over-driven LED does — but only part
    // way, so the eye keeps its colour.
    vec3 hot = mix(tint, vec3(1.0), 0.38);
    vec3 col = mix(tint, hot, core * depth) * mask * uBright;
    // The faint diffuse wash a matrix casts on its own diffuser.
    col += tint * halo * 0.08 * uBright;

    gl_FragColor = vec4(col, 1.0);
    #include <tonemapping_fragment>
    #include <colorspace_fragment>
  }
`;

export function screenMaterial(appearance: Appearance, pitch: number): THREE.ShaderMaterial {
  return new THREE.ShaderMaterial({
    vertexShader: VERT,
    fragmentShader: FRAG,
    uniforms: {
      uColor: { value: new THREE.Color(appearance.glow).multiplyScalar(2.0) },
      uAlarm: { value: new THREE.Color("#ff2a1f").multiplyScalar(2.2) },
      uAlarmMix: { value: 0 },
      uL: { value: new THREE.Vector4() },
      uLlid: { value: new THREE.Vector4() },
      uR: { value: new THREE.Vector4() },
      uRlid: { value: new THREE.Vector4() },
      uCross: { value: 0 },
      uBright: { value: 1 },
      uPitch: { value: pitch },
      uOffset: { value: new THREE.Vector2() },
    },
    toneMapped: true,
  });
}

// --- expressions ------------------------------------------------------------------------

/** The resting eye for each eye style, centred on the origin. */
function restingEye(style: Appearance["eyes"]): Omit<EyeShape, "x" | "y"> {
  switch (style) {
    case "round":
      return { w: 0.072, h: 0.072, round: 1, top: 0, bottom: 0, tilt: 0 };
    case "square":
      return { w: 0.074, h: 0.064, round: 0.3, top: 0, bottom: 0, tilt: 0 };
    case "dot":
      return { w: 0.04, h: 0.04, round: 1, top: 0, bottom: 0, tilt: 0 };
    case "visor":
      return { w: 0.2, h: 0.034, round: 1, top: 0, bottom: 0, tilt: 0 };
    default:
      return { w: 0.055, h: 0.088, round: 0.95, top: 0, bottom: 0, tilt: 0 };
  }
}

/** A slow random walk for where the eyes rest: a glance, a hold, another glance. */
function glance(t: number, seed: number): [number, number] {
  const slot = Math.floor((t + seed) / 2.3);
  const r1 = Math.sin(slot * 12.9898 + seed) * 43758.5453;
  const r2 = Math.sin(slot * 78.233 + seed) * 12345.6789;
  const fx = r1 - Math.floor(r1);
  const fy = r2 - Math.floor(r2);
  // Most of the time near centre; now and then a proper look to the side.
  const x = fx < 0.5 ? 0 : (fx - 0.75) * 0.11;
  const y = fy < 0.6 ? 0 : (fy - 0.8) * 0.06;
  return [x, y];
}

function blink(t: number, every: number, seed: number): number {
  const c = (t + seed * 0.37) % every;
  if (c > 0.16) return 0;
  return Math.sin((c / 0.16) * Math.PI);
}

/**
 * The target face for a mood at time `t`. Positions are relative to the face centre;
 * `spacing` is half the distance between the eyes.
 */
export function targetFace(
  mood: Mood,
  style: Appearance["eyes"],
  spacing: number,
  t: number,
  seed: number,
): Face {
  const base = restingEye(style);
  const single = style === "visor";
  const lx = single ? 0 : -spacing;
  const rx = single ? 0 : spacing;
  const left: EyeShape = { ...base, x: lx, y: 0 };
  const right: EyeShape = { ...base, x: rx, y: 0 };
  const f: Face = { left, right, cross: 0, bright: 1, alarm: 0 };
  const look = (x: number, y: number) => {
    left.x += x;
    right.x += x;
    left.y += y;
    right.y += y;
  };
  const lids = (top: number, bottom: number) => {
    left.top = right.top = top;
    left.bottom = right.bottom = bottom;
  };
  const scale = (s: number, eye?: EyeShape) => {
    for (const e of eye ? [eye] : [left, right]) {
      e.w *= s;
      e.h *= s;
    }
  };

  switch (mood) {
    case "idle": {
      const [gx, gy] = glance(t, seed);
      look(gx, gy);
      const b = blink(t, 4.2, seed);
      lids(b * 0.95, b * 0.05);
      break;
    }
    case "thinking": {
      // Up and to one side, one eye narrowed: weighing something.
      look(0.035 + 0.012 * Math.sin(t * 0.9), 0.03);
      right.bottom = 0.28;
      left.top = 0.12;
      const b = blink(t, 3.3, seed);
      left.top = Math.max(left.top, b * 0.95);
      right.top = b * 0.95;
      break;
    }
    case "browsing": {
      // Reading: a sweep across, a quick return, again — with a slight squint.
      const c = (t * 0.55) % 1;
      const x = c < 0.8 ? -0.05 + (c / 0.8) * 0.1 : 0.05 - ((c - 0.8) / 0.2) * 0.1;
      look(x, -0.008);
      lids(0.12, 0.08);
      break;
    }
    case "clicking": {
      const c = (t % 0.8) / 0.8;
      const hit = c < 0.18 ? Math.sin((c / 0.18) * Math.PI) : 0;
      look(0, -0.012);
      lids(0.1 + hit * 0.45, hit * 0.3);
      break;
    }
    case "typing":
      // Focused: looking down at the work, lids lowered.
      look(0.01 * Math.sin(t * 2.3), -0.022);
      lids(0.38, 0.12);
      break;
    case "waiting":
      // One eye wide, the other half-closed and tilted: "…well?"
      look(0, 0.012);
      scale(1.12, left);
      right.top = 0.42;
      right.tilt = -0.18;
      left.tilt = 0.06;
      break;
    case "error":
      f.cross = 1;
      f.alarm = 1;
      break;
    case "happy":
      // Half-moons: the bottom lid rises in a curve.
      lids(0, 0.55);
      look(0, 0.01 + 0.006 * Math.sin(t * 5));
      break;
    case "creating":
      scale(1.12);
      look(0.03 * Math.sin(t * 2.8), 0.01);
      f.bright = 1.2;
      break;
    case "delegating":
      // Looking across at someone and talking: a glance and a nod.
      look(0.045, 0.004 * Math.sin(t * 5));
      lids(0.08 + 0.06 * Math.max(0, Math.sin(t * 5)), 0.06);
      break;
    case "remembering":
      // Eyes gently closed, content: both lids meet in a thin, upturned arc.
      lids(0.42, 0.5);
      f.bright = 1 + 0.25 * Math.sin(t * 2);
      break;
    case "sleeping":
      lids(0.55, 0.42);
      f.bright = 0.35 + 0.1 * Math.sin(t * 1.0);
      break;
    case "stopped":
      lids(0.6, 0.38);
      look(0, -0.02);
      f.bright = 0.15;
      break;
  }
  return f;
}

const ease = (cur: number, target: number, k: number) => cur + (target - cur) * k;

/** Ease every number in `face` toward `target`. Blinks move faster than glances. */
export function easeFace(face: Face, target: Face, dt: number): void {
  const slow = Math.min(1, dt * 9);
  const fast = Math.min(1, dt * 22);
  for (const side of ["left", "right"] as const) {
    const a = face[side];
    const b = target[side];
    a.x = ease(a.x, b.x, Math.min(1, dt * 14));
    a.y = ease(a.y, b.y, Math.min(1, dt * 14));
    a.w = ease(a.w, b.w, slow);
    a.h = ease(a.h, b.h, slow);
    a.round = ease(a.round, b.round, slow);
    a.top = ease(a.top, b.top, fast);
    a.bottom = ease(a.bottom, b.bottom, fast);
    a.tilt = ease(a.tilt, b.tilt, slow);
  }
  face.cross = ease(face.cross, target.cross, slow);
  face.bright = ease(face.bright, target.bright, slow);
  face.alarm = ease(face.alarm, target.alarm, slow);
}

export function cloneFace(f: Face): Face {
  return { ...f, left: { ...f.left }, right: { ...f.right } };
}

export function applyFace(
  material: THREE.ShaderMaterial,
  face: Face,
  originX: number,
  originY: number,
): void {
  const u = material.uniforms;
  const put = (v: THREE.Vector4, lid: THREE.Vector4, e: EyeShape) => {
    v.set(e.x + originX, e.y + originY, Math.max(0.004, e.w), Math.max(0.004, e.h));
    lid.set(Math.min(1, Math.max(0, e.top)), Math.min(1, Math.max(0, e.bottom)), e.tilt, e.round);
  };
  put(u.uL.value, u.uLlid.value, face.left);
  put(u.uR.value, u.uRlid.value, face.right);
  u.uCross.value = face.cross;
  u.uBright.value = face.bright;
  u.uAlarmMix.value = face.alarm;
}
