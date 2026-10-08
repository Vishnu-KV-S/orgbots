import * as THREE from "three";
import type { Appearance } from "./appearance";
import type { Mood } from "./mood";

/**
 * The display: soft LED eyes, drawn by a shader on the bot's screen.
 *
 * Every bot has the same eyes. Each is a signed-distance shape — a fully rounded
 * box with curved top and bottom lids and a tilt — rendered as solid light with a
 * soft edge and a faint bloom. An expression is a handful of numbers (lids, size,
 * tilt, gaze), eased toward their targets every frame, so a blink closes like a lid
 * and a smile curves up instead of switching sprites.
 *
 * **The eyes can become dots.** When a bot is thinking, the two eyes morph into four
 * small dots that ripple in sequence, and morph back when it is done. It is the same
 * distance field, blended between the two shapes, so the change is a melt rather than
 * a cut.
 */

export interface EyeShape {
  x: number;
  y: number;
  /** Half width and half height, in body units. */
  w: number;
  h: number;
  /** How far each lid has closed, 0..1 of the eye's height. */
  top: number;
  bottom: number;
  tilt: number;
}

export interface Face {
  left: EyeShape;
  right: EyeShape;
  /** 0 normal, 1 X-eyes. */
  cross: number;
  /** 0 eyes, 1 four thinking dots. */
  dots: number;
  /** Each thinking dot's lift, for the ripple. */
  dotLift: [number, number, number, number];
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
  uniform vec4 uL; uniform vec3 uLlid;   // x, y, w, h | top, bottom, tilt
  uniform vec4 uR; uniform vec3 uRlid;
  uniform float uCross;
  uniform float uDots;
  uniform vec4 uDotLift;
  uniform vec2 uDotCentre;
  uniform float uBright;
  varying vec3 vPos;

  float eye(vec2 p, vec4 e, vec3 lid) {
    vec2 q = p - e.xy;
    float c = cos(lid.z), s = sin(lid.z);
    q = vec2(c * q.x + s * q.y, -s * q.x + c * q.y);
    // Fully rounded: the corner radius is the whole of the shorter side.
    float r = min(e.z, e.w);
    vec2 d = abs(q) - e.zw + r;
    float shape = length(max(d, 0.0)) + min(max(d.x, d.y), 0.0) - r;
    float xn = clamp(q.x / max(e.z, 0.001), -1.0, 1.0);
    // Curved lids: the top droops at the corners, the bottom arches up in the middle
    // — which is what makes a rising bottom lid a smile.
    float topY = e.w - lid.x * 2.0 * e.w;
    float topCut = q.y - (topY - 0.5 * lid.x * e.w * xn * xn);
    float botY = -e.w + lid.y * 2.0 * e.w;
    float botCut = botY + 0.9 * lid.y * e.w * (1.0 - xn * xn) - q.y;
    // A rounded intersection, so a half-closed eye keeps soft corners.
    float k = 0.012;
    float a = max(topCut, botCut);
    float h = clamp(0.5 + 0.5 * (a - shape) / k, 0.0, 1.0);
    return mix(shape, a, h) + k * h * (1.0 - h);
  }

  float capsule(vec2 p, vec2 a, vec2 b, float r) {
    vec2 pa = p - a, ba = b - a;
    float h = clamp(dot(pa, ba) / dot(ba, ba), 0.0, 1.0);
    return length(pa - ba * h) - r;
  }

  float crossShape(vec2 p, vec4 e) {
    vec2 q = p - e.xy;
    float k = min(e.z, e.w) * 0.8;
    float r = k * 0.26;
    return min(capsule(q, vec2(-k, -k), vec2(k, k), r), capsule(q, vec2(-k, k), vec2(k, -k), r));
  }

  float thinkingDots(vec2 p) {
    float d = 1e3;
    for (int i = 0; i < 4; i++) {
      float x = (float(i) - 1.5) * 0.078;
      vec2 c = uDotCentre + vec2(x, uDotLift[i]);
      d = min(d, length(p - c) - 0.027);
    }
    return d;
  }

  float face(vec2 p) {
    float eyes = min(eye(p, uL, uLlid), eye(p, uR, uRlid));
    float crossed = min(crossShape(p, uL), crossShape(p, uR));
    float base = mix(eyes, crossed, uCross);
    return mix(base, thinkingDots(p), uDots);
  }

  // Every smoothstep here is written low-edge-first: GLSL leaves a reversed one
  // undefined, and some GPUs return 0 for it.
  void main() {
    vec2 p = vPos.xy;
    float d = face(p);
    float aa = max(fwidth(d), 0.0015) + 0.002;
    float body = 1.0 - smoothstep(-aa, aa, d);
    // A faint bloom just past the edge — enough to read as light, not a halo.
    float bloom = exp(-max(d, 0.0) / 0.01) * 0.14 * (1.0 - body);
    float depth = clamp(-d / 0.04, 0.0, 1.0);

    vec3 tint = mix(uColor, uAlarm, uAlarmMix);
    vec3 core = mix(tint, vec3(1.0), 0.14);
    vec3 col = (mix(tint, core, depth) * body + tint * bloom) * uBright;

    gl_FragColor = vec4(col, 1.0);
    #include <tonemapping_fragment>
    #include <colorspace_fragment>
  }
`;

export function screenMaterial(appearance: Appearance): THREE.ShaderMaterial {
  return new THREE.ShaderMaterial({
    vertexShader: VERT,
    fragmentShader: FRAG,
    uniforms: {
      uColor: { value: new THREE.Color(appearance.glow).multiplyScalar(2.6) },
      uAlarm: { value: new THREE.Color("#ff3a2e").multiplyScalar(2.6) },
      uAlarmMix: { value: 0 },
      uL: { value: new THREE.Vector4() },
      uLlid: { value: new THREE.Vector3() },
      uR: { value: new THREE.Vector4() },
      uRlid: { value: new THREE.Vector3() },
      uCross: { value: 0 },
      uDots: { value: 0 },
      uDotLift: { value: new THREE.Vector4() },
      uDotCentre: { value: new THREE.Vector2() },
      uBright: { value: 1 },
    },
    toneMapped: true,
  });
}

// --- expressions ------------------------------------------------------------------------

/** The one resting eye every bot has: a soft, upright oval. */
const RESTING = { w: 0.058, h: 0.084, top: 0, bottom: 0, tilt: 0 };

/** A slow random walk for where the eyes rest: a glance, a hold, another glance. */
function glance(t: number, seed: number): [number, number] {
  const slot = Math.floor((t + seed) / 2.3);
  const r1 = Math.sin(slot * 12.9898 + seed) * 43758.5453;
  const r2 = Math.sin(slot * 78.233 + seed) * 12345.6789;
  const fx = r1 - Math.floor(r1);
  const fy = r2 - Math.floor(r2);
  const x = fx < 0.5 ? 0 : (fx - 0.75) * 0.1;
  const y = fy < 0.6 ? 0 : (fy - 0.8) * 0.05;
  return [x, y];
}

function blink(t: number, every: number, seed: number): number {
  const c = (t + seed * 0.37) % every;
  if (c > 0.18) return 0;
  return Math.sin((c / 0.18) * Math.PI);
}

/** The target face for a mood at time `t`, relative to the face centre. */
export function targetFace(mood: Mood, spacing: number, t: number, seed: number): Face {
  const left: EyeShape = { ...RESTING, x: -spacing, y: 0 };
  const right: EyeShape = { ...RESTING, x: spacing, y: 0 };
  const f: Face = {
    left,
    right,
    cross: 0,
    dots: 0,
    dotLift: [0, 0, 0, 0],
    bright: 1,
    alarm: 0,
  };
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
    case "thinking":
      // Four dots, rippling one after another.
      f.dots = 1;
      f.dotLift = [0, 1, 2, 3].map(
        (i) => 0.02 * Math.max(0, Math.sin(t * 5.5 - i * 0.75)),
      ) as Face["dotLift"];
      break;
    case "browsing": {
      // Reading: a sweep across, a quick return, again — with a slight squint.
      const c = (t * 0.55) % 1;
      const x = c < 0.8 ? -0.045 + (c / 0.8) * 0.09 : 0.045 - ((c - 0.8) / 0.2) * 0.09;
      look(x, -0.006);
      lids(0.12, 0.08);
      break;
    }
    case "clicking": {
      const c = (t % 0.8) / 0.8;
      const hit = c < 0.18 ? Math.sin((c / 0.18) * Math.PI) : 0;
      look(0, -0.01);
      lids(0.1 + hit * 0.42, hit * 0.28);
      break;
    }
    case "typing":
      look(0.01 * Math.sin(t * 2.3), -0.02);
      lids(0.36, 0.12);
      break;
    case "waiting":
      // One eye wide, the other half-closed and tilted: "…well?"
      look(0, 0.01);
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
      lids(0, 0.55);
      look(0, 0.008 + 0.005 * Math.sin(t * 5));
      break;
    case "creating":
      scale(1.12);
      look(0.03 * Math.sin(t * 2.8), 0.008);
      break;
    case "delegating":
      look(0.04, 0.004 * Math.sin(t * 5));
      lids(0.08 + 0.06 * Math.max(0, Math.sin(t * 5)), 0.06);
      break;
    case "remembering":
      lids(0.42, 0.5);
      f.bright = 1 + 0.15 * Math.sin(t * 2);
      break;
    case "sleeping":
      lids(0.55, 0.42);
      f.bright = 0.4 + 0.1 * Math.sin(t * 1.0);
      break;
    case "stopped":
      lids(0.6, 0.38);
      look(0, -0.02);
      f.bright = 0.2;
      break;
  }
  return f;
}

const ease = (cur: number, target: number, k: number) => cur + (target - cur) * k;

/** Ease every number in `face` toward `target`. Blinks move faster than glances. */
export function easeFace(face: Face, target: Face, dt: number): void {
  const slow = Math.min(1, dt * 8);
  const fast = Math.min(1, dt * 20);
  const gaze = Math.min(1, dt * 12);
  for (const side of ["left", "right"] as const) {
    const a = face[side];
    const b = target[side];
    a.x = ease(a.x, b.x, gaze);
    a.y = ease(a.y, b.y, gaze);
    a.w = ease(a.w, b.w, slow);
    a.h = ease(a.h, b.h, slow);
    a.top = ease(a.top, b.top, fast);
    a.bottom = ease(a.bottom, b.bottom, fast);
    a.tilt = ease(a.tilt, b.tilt, slow);
  }
  face.cross = ease(face.cross, target.cross, slow);
  // The morph between eyes and dots is the slowest thing on the face, so it reads
  // as a transformation rather than a flicker.
  face.dots = ease(face.dots, target.dots, Math.min(1, dt * 5));
  for (let i = 0; i < 4; i++) face.dotLift[i] = ease(face.dotLift[i], target.dotLift[i], fast);
  face.bright = ease(face.bright, target.bright, slow);
  face.alarm = ease(face.alarm, target.alarm, slow);
}

export function cloneFace(f: Face): Face {
  return {
    ...f,
    left: { ...f.left },
    right: { ...f.right },
    dotLift: [...f.dotLift] as Face["dotLift"],
  };
}

export function applyFace(
  material: THREE.ShaderMaterial,
  face: Face,
  originX: number,
  originY: number,
): void {
  const u = material.uniforms;
  const put = (v: THREE.Vector4, lid: THREE.Vector3, e: EyeShape) => {
    v.set(e.x + originX, e.y + originY, Math.max(0.004, e.w), Math.max(0.004, e.h));
    lid.set(Math.min(1, Math.max(0, e.top)), Math.min(1, Math.max(0, e.bottom)), e.tilt);
  };
  put(u.uL.value, u.uLlid.value, face.left);
  put(u.uR.value, u.uRlid.value, face.right);
  u.uCross.value = face.cross;
  u.uDots.value = face.dots;
  u.uDotLift.value.set(...face.dotLift);
  u.uDotCentre.value.set(originX, originY);
  u.uBright.value = face.bright;
  u.uAlarmMix.value = face.alarm;
}
