import type { Appearance } from "./api";

/**
 * A bot's look, derived exactly as `ui/features/bots/avatar/appearance.ts` derives
 * it, so a bot has the same colours on the phone as in the browser. A bot that saved
 * nothing gets one from its id: stable across devices, different for every bot.
 */

const SHAPES: Appearance["shape"][] = ["orb", "cube", "capsule", "pod", "tv"];
const EYES: Appearance["eyes"][] = ["pill", "round", "square", "visor", "dot"];
const TOPS: Appearance["top"][] = ["ring", "knobs", "antenna", "ears", "halo", "none"];
const FINISHES: Appearance["finish"][] = ["gloss", "pearl", "metal", "matte"];

const BODY_COLORS = [
  "#ecebe7",
  "#eceaf3",
  "#c9b8f0",
  "#9aa3b5",
  "#2c2f3a",
  "#f2c6d9",
  "#b9e3f5",
  "#cfe8c4",
  "#f5d9a8",
  "#e9b06b",
  "#7a5cff",
];

const GLOW_COLORS = [
  "#f5a623",
  "#e040fb",
  "#ff4fa3",
  "#7c4dff",
  "#18c8ff",
  "#3dffb0",
  "#c6ff3d",
  "#ffb02e",
  "#ff5a4f",
  "#ffffff",
];

export const PRESETS: Record<string, Appearance> = {
  Orbit: {
    shape: "orb",
    body: "#ecebe7",
    glow: "#f5a623",
    eyes: "pill",
    top: "ring",
    finish: "matte",
  },
  Cubey: {
    shape: "cube",
    body: "#d8d4cc",
    glow: "#ff4fa3",
    eyes: "pill",
    top: "knobs",
    finish: "matte",
  },
  Sprout: {
    shape: "capsule",
    body: "#cfe0c8",
    glow: "#3dffb0",
    eyes: "pill",
    top: "antenna",
    finish: "matte",
  },
  Beacon: {
    shape: "pod",
    body: "#3a3b3f",
    glow: "#18c8ff",
    eyes: "pill",
    top: "halo",
    finish: "matte",
  },
  Telly: {
    shape: "tv",
    body: "#e9dfcf",
    glow: "#f5a623",
    eyes: "pill",
    top: "ears",
    finish: "matte",
  },
};

/** mulberry32, as on the web. */
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
    shape: pick(r, SHAPES),
    body: pick(r, BODY_COLORS),
    glow: pick(r, GLOW_COLORS),
    eyes: pick(r, EYES),
    top: pick(r, TOPS),
    finish: pick(r, FINISHES),
  };
}

export function appearanceFor(bot: {
  id: string;
  appearance?: Partial<Appearance> | null;
}): Appearance {
  const saved = bot.appearance ?? {};
  if (Object.keys(saved).length === 0) return randomAppearance(bot.id);
  return { ...randomAppearance(bot.id), ...saved } as Appearance;
}

/** Is a body colour dark enough that its face plate should be light-on-dark? */
export function isDark(hex: string): boolean {
  const n = parseInt(hex.replace("#", ""), 16);
  const r = (n >> 16) & 255;
  const g = (n >> 8) & 255;
  const b = n & 255;
  return 0.299 * r + 0.587 * g + 0.114 * b < 110;
}
