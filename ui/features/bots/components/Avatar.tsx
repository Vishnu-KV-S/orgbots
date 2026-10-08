"use client";

import { type Appearance, BotFace, FlatFace, type Mood, appearanceFor } from "../avatar";

/**
 * A bot's face. `live` draws the animated 3D bot (in the shared canvas); otherwise a
 * flat CSS face in the same colours — for places with many small faces at once,
 * like every bot message in a long transcript.
 */
export function Avatar({
  bot,
  appearance,
  mood = "idle",
  size = 32,
  live = false,
}: {
  bot?: { id: string; appearance?: Partial<Appearance> | null };
  appearance?: Appearance;
  mood?: Mood;
  size?: number;
  live?: boolean;
}) {
  const look = appearance ?? appearanceFor(bot ?? { id: "anonymous" });
  if (!live) return <FlatFace appearance={look} size={size} />;
  return (
    <BotFace
      appearance={look}
      mood={mood}
      size={size}
      seed={bot?.id ?? ""}
      framing={size < 80 ? "head" : "full"}
    />
  );
}
