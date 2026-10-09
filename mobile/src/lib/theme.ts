import { useColorScheme } from "react-native";

/**
 * Design tokens, mirroring `ui/styles/tokens.css`: monochrome and flat, ranked by
 * brightness alone. The only colour on screen is the bots themselves.
 *
 * The dark theme is true black, the way a phone's OLED wants it; the light theme is
 * the same ranking inverted.
 */

export interface Palette {
  bg: string;
  surface: string;
  raised: string;
  pressed: string;
  line: string;
  lineStrong: string;
  text: string;
  textDim: string;
  textFaint: string;
  /** The send button and other emphasis: near-white on dark, near-black on light. */
  accent: string;
  onAccent: string;
  bubble: string;
  scheme: "dark" | "light";
}

export const dark: Palette = {
  bg: "#000000",
  surface: "#0c0c0c",
  raised: "#161616",
  pressed: "#222222",
  line: "#1f1f1f",
  lineStrong: "#2e2e2e",
  text: "#f1f1f1",
  textDim: "#a3a3a3",
  textFaint: "#6b6b6b",
  accent: "#f1f1f1",
  onAccent: "#000000",
  bubble: "#1c1c1c",
  scheme: "dark",
};

export const light: Palette = {
  bg: "#ffffff",
  surface: "#fafafa",
  raised: "#f2f2f2",
  pressed: "#e6e6e6",
  line: "#ececec",
  lineStrong: "#d9d9d9",
  text: "#0d0d0d",
  textDim: "#5c5c5c",
  textFaint: "#9a9a9a",
  accent: "#0d0d0d",
  onAccent: "#ffffff",
  bubble: "#f1f1f1",
  scheme: "light",
};

export function usePalette(): Palette {
  return useColorScheme() === "light" ? light : dark;
}

export const radius = { sm: 10, md: 14, lg: 22, pill: 999 };

export const font = {
  title: 28,
  heading: 17,
  body: 16,
  small: 13,
  tiny: 11,
};
