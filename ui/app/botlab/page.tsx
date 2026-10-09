"use client";

// A visual harness for the 3D bots: every preset side by side, no data, no API. Not
// linked from the app. Query: mood, size, only=<index>, extra=1 (more top pieces),
// head=1 (head framing), flat=1 and small=1 (the small faces lists use), bg=<colour>.
import { useSearchParams } from "next/navigation";
import { Suspense } from "react";
import { BotFace, BotStage, FlatFace, PRESETS, type Appearance, type Mood } from "@/features/bots/avatar";

function Lab() {
  const q = useSearchParams();
  const mood = (q.get("mood") ?? "idle") as Mood;
  const size = Number(q.get("size") ?? 320);
  const only = q.get("only");
  const extra: Appearance[] = [
    { ...PRESETS[0].appearance, top: "none" },
    { ...PRESETS[1].appearance, top: "antenna", shape: "cube" },
    { ...PRESETS[2].appearance, top: "halo" },
    { ...PRESETS[3].appearance, top: "ring" },
    { ...PRESETS[4].appearance, top: "knobs" },
  ];
  const all = [...PRESETS.map((p) => p.appearance), ...(q.get("extra") ? extra : [])];
  const list = only ? [all[Number(only)]] : all;
  return (
    <BotStage>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 8, padding: 8, background: q.get("bg") ?? "#ffffff" }}>
        {list.map((a, i) => (
          <BotFace key={i} appearance={a} mood={mood} size={size} detail="high" seed={String(i)} framing={q.get("head") ? "head" : "full"} />
        ))}
        {q.get("flat") &&
          [24, 34, 64].flatMap((sz) => list.map((a, i) => <FlatFace key={`f${sz}${i}`} appearance={a} size={sz} />))}
        {q.get("small") &&
          list.map((a, i) => <BotFace key={`s${i}`} appearance={a} mood={mood} size={40} seed={String(i)} framing="head" />)}
      </div>
    </BotStage>
  );
}

export default function Page() {
  return (
    <Suspense>
      <Lab />
    </Suspense>
  );
}
