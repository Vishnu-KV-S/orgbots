import * as THREE from "three";
import { RoundedBoxGeometry } from "three/examples/jsm/geometries/RoundedBoxGeometry.js";

/**
 * The bots' geometry, built once and shared by every bot on screen.
 *
 * Everything is generated from smooth profiles and finely subdivided, because a
 * glossy finish shows every facet: a reflection slides across a curve, and if the
 * curve is a few dozen flat triangles the reflection breaks up along their edges.
 */

const cache = new Map<string, THREE.BufferGeometry>();
function cached<T extends THREE.BufferGeometry>(key: string, make: () => T): T {
  let g = cache.get(key) as T | undefined;
  if (!g) {
    g = make();
    cache.set(key, g);
  }
  return g;
}

export const sphere = (r: number, w = 64, h = 48) =>
  cached(`sphere:${r}:${w}:${h}`, () => new THREE.SphereGeometry(r, w, h));
export const torus = (r: number, tube: number) =>
  cached(`torus:${r}:${tube}`, () => new THREE.TorusGeometry(r, tube, 32, 128));
export const capsule = (r: number, length: number, radial = 48) =>
  cached(`capsule:${r}:${length}:${radial}`, () => new THREE.CapsuleGeometry(r, length, 16, radial));
/** A box with every edge and corner rounded: subdivided, so its faces stay smooth. */
export const roundedBox = (w: number, h: number, d: number, r: number) =>
  cached(`box:${w}:${h}:${d}:${r}`, () => new RoundedBoxGeometry(w, h, d, 16, r));

/** What a panel is bent onto: a flat face, a sphere, or a (slightly tapered) cylinder. */
export type Surface =
  | { kind: "flat" }
  | { kind: "sphere"; radius: number }
  | { kind: "cylinder"; radius: number; taper?: number };

/** Map a point on the flat panel — `z` is height above the surface — onto the surface,
 * with the panel's centre at the origin. `x` and `y` are arc lengths, so a panel keeps
 * its size whatever it is bent round. */
function bend(x: number, y: number, z: number, s: Surface): [number, number, number] {
  switch (s.kind) {
    case "flat":
      return [x, y, z];
    case "sphere": {
      const R = s.radius;
      const lon = x / R;
      const lat = y / R;
      const r = R + z;
      return [
        r * Math.sin(lon) * Math.cos(lat),
        r * Math.sin(lat),
        r * Math.cos(lon) * Math.cos(lat) - R,
      ];
    }
    case "cylinder": {
      const R = s.radius + (s.taper ?? 0) * y;
      const lon = x / R;
      const r = R + z;
      return [r * Math.sin(lon), y, r * Math.cos(lon) - s.radius];
    }
  }
}

interface Rim {
  x: number;
  y: number;
  nx: number;
  ny: number;
}

/** Points round a rounded rectangle, anticlockwise, each with its outward normal. The
 * straight edges are subdivided too, so they bend smoothly round a curved body. */
function outline(hw: number, hh: number, r: number): Rim[] {
  const corners: [number, number, number][] = [
    [hw - r, hh - r, 0],
    [-hw + r, hh - r, Math.PI / 2],
    [-hw + r, -hh + r, Math.PI],
    [hw - r, -hh + r, (3 * Math.PI) / 2],
  ];
  const pts: Rim[] = [];
  for (let c = 0; c < 4; c++) {
    const [cx, cy, a0] = corners[c];
    for (let i = 0; i < 16; i++) {
      const a = a0 + (i / 16) * (Math.PI / 2);
      pts.push({ x: cx + r * Math.cos(a), y: cy + r * Math.sin(a), nx: Math.cos(a), ny: Math.sin(a) });
    }
    const nx = Math.cos(a0 + Math.PI / 2);
    const ny = Math.sin(a0 + Math.PI / 2);
    const [ex, ey] = corners[(c + 1) % 4];
    const sx = cx + r * nx;
    const sy = cy + r * ny;
    const tx = ex + r * nx;
    const ty = ey + r * ny;
    const n = Math.max(1, Math.ceil(Math.hypot(tx - sx, ty - sy) / 0.02));
    for (let i = 0; i < n; i++) {
      pts.push({ x: sx + ((tx - sx) * i) / n, y: sy + ((ty - sy) * i) / n, nx, ny });
    }
  }
  return pts;
}

/**
 * A rounded slab — a display, the glass over it, the seam round it — with a fully
 * rounded front edge, bent onto a surface. The front is `depth` above the surface and
 * the sides run `sink` below it, into the body, so no gap opens where it meets a
 * curve. UVs carry the flat, unbent position: the eye shader draws in those units.
 */
export function slab(
  w: number,
  h: number,
  r: number,
  depth: number,
  bevel: number,
  surface: Surface,
  sink = 0.04,
): THREE.BufferGeometry {
  const key = `slab:${w}:${h}:${r}:${depth}:${bevel}:${JSON.stringify(surface)}:${sink}`;
  return cached(key, () => {
    const b = Math.min(bevel, r * 0.9);
    const rim = outline(w / 2 - b, h / 2 - b, r - b);
    const n = rim.length;
    const pos: number[] = [];
    const uv: number[] = [];
    const put = (x: number, y: number, z: number) => {
      pos.push(...bend(x, y, z, surface));
      uv.push(x, y);
    };
    put(0, 0, depth);
    const INNER = 16;
    for (let k = 1; k <= INNER; k++) {
      const s = k / INNER;
      for (const p of rim) put(p.x * s, p.y * s, depth);
    }
    const ROUND = 10;
    for (let j = 1; j <= ROUND; j++) {
      const a = (j / ROUND) * (Math.PI / 2);
      const out = b * Math.sin(a);
      for (const p of rim) put(p.x + p.nx * out, p.y + p.ny * out, depth - b * (1 - Math.cos(a)));
    }
    for (const p of rim) put(p.x + p.nx * b, p.y + p.ny * b, -sink);

    const index: number[] = [];
    for (let i = 0; i < n; i++) index.push(0, 1 + i, 1 + ((i + 1) % n));
    const rings = INNER + ROUND + 1;
    for (let k = 0; k < rings - 1; k++) {
      const a = 1 + k * n;
      const c = 1 + (k + 1) * n;
      for (let i = 0; i < n; i++) {
        const j = (i + 1) % n;
        index.push(a + i, c + i, c + j, a + i, c + j, a + j);
      }
    }
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
    g.setAttribute("uv", new THREE.Float32BufferAttribute(uv, 2));
    g.setIndex(index);
    g.computeVertexNormals();
    return g;
  });
}

/** A turned profile, smooth all the way round. */
function lathe(key: string, profile: () => [number, number][], segments = 96) {
  return cached(key, () => {
    const pts = profile().map(([r, y]) => new THREE.Vector2(Math.max(0.0001, r), y));
    return new THREE.LatheGeometry(pts, segments);
  });
}

/** The pod: one turned profile — dome, gently tapered sides, a rounded foot. */
export function podShell() {
  return lathe(
    "pod",
    () => {
      const pts: [number, number][] = [[0, -0.41]];
      for (let i = 0; i <= 16; i++) {
        const a = -Math.PI / 2 + (i / 16) * (Math.PI / 2);
        pts.push([0.42 + 0.08 * Math.cos(a), -0.33 + 0.08 * Math.sin(a)]);
      }
      for (let i = 1; i <= 8; i++) pts.push([0.5 + (0.05 * i) / 8, -0.33 + (0.38 * i) / 8]);
      for (let i = 1; i <= 48; i++) {
        const a = (i / 48) * (Math.PI / 2);
        pts.push([0.55 * Math.cos(a), 0.05 + 0.55 * Math.sin(a)]);
      }
      return pts;
    },
    160,
  );
}

/** A side ear: a round pod with a softly rounded rim and a gently domed face. Its axis
 * is +y; it sinks below y = 0 into the body it is mounted on. */
export function earPod() {
  return lathe("ear", () => {
    const R = 0.15;
    const H = 0.06;
    const e = 0.03;
    const pts: [number, number][] = [
      [0, -0.06],
      [R, -0.06],
      [R, H - e],
    ];
    for (let i = 1; i <= 12; i++) {
      const a = (i / 12) * (Math.PI / 2);
      pts.push([R - e + e * Math.cos(a), H - e + e * Math.sin(a)]);
    }
    for (let i = 1; i <= 10; i++) {
      const r = (R - e) * (1 - i / 10);
      pts.push([r, H + 0.008 * (1 - (r / (R - e)) ** 2)]);
    }
    return pts;
  });
}

/** A cat's ear: a cone with a blunt, rounded tip. Base on y = 0, sinking a little below. */
export function catEar() {
  return lathe(
    "catEar",
    () => {
      const pts: [number, number][] = [[0, -0.05], [0.12, -0.05]];
      for (let i = 0; i <= 32; i++) {
        const t = i / 32;
        pts.push([0.12 * Math.cos((Math.PI / 2) * t) ** 0.75, 0.2 * t]);
      }
      return pts;
    },
    64,
  );
}
