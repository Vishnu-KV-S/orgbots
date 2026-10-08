/**
 * A bot's 3D body, as data. Mirrors `runtime.domain.bots.BotAppearance` — the server
 * validates the same vocabularies, so anything the designer can produce is storable.
 *
 * A bot with no saved appearance (`{}`) gets one derived from its id: stable across
 * reloads and devices, and different for every helper a bot creates for itself.
 */

export type Shape = "orb" | "cube" | "capsule" | "pod" | "tv";
export type Eyes = "pill" | "round" | "square" | "visor" | "dot";
export type Top = "ring" | "knobs" | "antenna" | "ears" | "halo" | "none";
export type Finish = "gloss" | "matte" | "metal" | "pearl";

export interface Appearance {
  shape: Shape;
  body: string;
  glow: string;
  eyes: Eyes;
  top: Top;
  finish: Finish;
}

export const SHAPES: { id: Shape; label: string }[] = [
  { id: "orb", label: "Orb" },
  { id: "cube", label: "Cube" },
  { id: "capsule", label: "Capsule" },
  { id: "pod", label: "Pod" },
  { id: "tv", label: "Retro TV" },
];

export const EYES: { id: Eyes; label: string }[] = [
  { id: "pill", label: "Pills" },
  { id: "round", label: "Round" },
  { id: "square", label: "Square" },
  { id: "visor", label: "Visor bar" },
  { id: "dot", label: "Dots" },
];

export const TOPS: { id: Top; label: string }[] = [
  { id: "ring", label: "Ring" },
  { id: "knobs", label: "Knobs" },
  { id: "antenna", label: "Antenna" },
  { id: "ears", label: "Ears" },
  { id: "halo", label: "Halo" },
  { id: "none", label: "None" },
];

export const FINISHES: { id: Finish; label: string }[] = [
  { id: "gloss", label: "Gloss" },
  { id: "pearl", label: "Pearl" },
  { id: "metal", label: "Metal" },
  { id: "matte", label: "Matte" },
];

export const BODY_COLORS = [
  "#ecebe7", // warm off-white
  "#eceaf3", // pearl white
  "#c9b8f0", // lilac
  "#9aa3b5", // brushed steel
  "#2c2f3a", // graphite
  "#f2c6d9", // blush
  "#b9e3f5", // ice
  "#cfe8c4", // mint
  "#f5d9a8", // sand
  "#e9b06b", // copper
  "#7a5cff", // violet
];

export const GLOW_COLORS = [
  "#f5a623", // anodised orange
  "#e040fb", // magenta
  "#ff4fa3", // hot pink
  "#7c4dff", // ultraviolet
  "#18c8ff", // cyan
  "#3dffb0", // mint
  "#c6ff3d", // lime
  "#ffb02e", // amber
  "#ff5a4f", // coral
  "#ffffff", // white
];

/** The two looks from the reference images, and a few more. */
/** Starting points. Every bot shares the finish and the eyes; these vary the rest. */
export const PRESETS: { name: string; appearance: Appearance }[] = [
  {
    name: "Orbit",
    appearance: {
      shape: "orb",
      body: "#ecebe7",
      glow: "#f5a623",
      eyes: "pill",
      top: "ring",
      finish: "matte",
    },
  },
  {
    name: "Cubey",
    appearance: {
      shape: "cube",
      body: "#d8d4cc",
      glow: "#ff4fa3",
      eyes: "pill",
      top: "knobs",
      finish: "matte",
    },
  },
  {
    name: "Sprout",
    appearance: {
      shape: "capsule",
      body: "#cfe0c8",
      glow: "#3dffb0",
      eyes: "pill",
      top: "antenna",
      finish: "matte",
    },
  },
  {
    name: "Beacon",
    appearance: {
      shape: "pod",
      body: "#3a3b3f",
      glow: "#18c8ff",
      eyes: "pill",
      top: "halo",
      finish: "matte",
    },
  },
  {
    name: "Telly",
    appearance: {
      shape: "tv",
      body: "#e9dfcf",
      glow: "#f5a623",
      eyes: "pill",
      top: "ears",
      finish: "matte",
    },
  },
];

export const DEFAULT_APPEARANCE: Appearance = PRESETS[0].appearance;

/** mulberry32: small, fast, good enough to pick colours deterministically. */
function rng(seed: number) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function hash(text: string): number {
  let h = 2166136261;
  for (let i = 0; i < text.length; i++) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

const pick = <T>(r: () => number, list: readonly T[]): T => list[Math.floor(r() * list.length)];

export function randomAppearance(seed?: string): Appearance {
  const r = seed ? rng(hash(seed)) : Math.random;
  return {
    shape: pick(r, SHAPES).id,
    body: pick(r, BODY_COLORS),
    glow: pick(r, GLOW_COLORS),
    eyes: pick(r, EYES).id,
    top: pick(r, TOPS).id,
    finish: pick(r, FINISHES).id,
  };
}

/** What to draw for a bot: what it saved, filled in from its id where it saved nothing. */
export function appearanceFor(bot: {
  id: string;
  appearance?: Partial<Appearance> | null;
}): Appearance {
  const saved = bot.appearance ?? {};
  if (Object.keys(saved).length === 0) return randomAppearance(bot.id);
  return { ...randomAppearance(bot.id), ...saved } as Appearance;
}
