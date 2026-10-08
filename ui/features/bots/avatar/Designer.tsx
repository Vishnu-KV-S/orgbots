"use client";

import { useEffect, useState } from "react";
import { cx } from "@/lib/cx";
import {
  type Appearance,
  BODY_COLORS,
  EYES,
  FINISHES,
  GLOW_COLORS,
  PRESETS,
  SHAPES,
  TOPS,
  randomAppearance,
} from "./appearance";
import { MOODS, type Mood } from "./mood";
import { BotFace } from "./Stage";

/**
 * Build a bot's body: presets, then every part, with the live 3D preview playing any
 * animation on demand — "what does it look like when it's typing?" is answered by
 * pressing Typing, not by waiting for it to type.
 */
export function Designer({
  value,
  onChange,
  standalone = false,
  previewSize = 200,
}: {
  value: Appearance;
  onChange: (next: Appearance) => void;
  /** True inside a dialog — see `BotFace.standalone`. */
  standalone?: boolean;
  previewSize?: number;
}) {
  const [mood, setMood] = useState<Mood>("idle");
  const [demo, setDemo] = useState(false);

  // "Play all": walk through every animation, two seconds each.
  useEffect(() => {
    if (!demo) return;
    let i = MOODS.findIndex((m) => m.id === mood);
    const timer = setInterval(() => {
      i = (i + 1) % MOODS.length;
      setMood(MOODS[i].id);
    }, 2200);
    return () => clearInterval(timer);
  }, [demo]); // eslint-disable-line react-hooks/exhaustive-deps

  const set = <K extends keyof Appearance>(key: K, v: Appearance[K]) =>
    onChange({ ...value, [key]: v });
  const hint = MOODS.find((m) => m.id === mood)?.hint;

  return (
    <div className="designer">
      <div className="designer-preview">
        <BotFace
          appearance={value}
          mood={mood}
          size={previewSize}
          detail="high"
          standalone={standalone}
        />
        <div className="designer-mood">
          <strong>{MOODS.find((m) => m.id === mood)?.label}</strong> — {hint}
        </div>
        <div className="designer-moods" role="radiogroup" aria-label="Preview an animation">
          {MOODS.map((m) => (
            <button
              key={m.id}
              type="button"
              className={cx("mchip", mood === m.id && "on")}
              onClick={() => {
                setDemo(false);
                setMood(m.id);
              }}
              title={m.hint}
            >
              {m.label}
            </button>
          ))}
          <button
            type="button"
            className={cx("mchip", demo && "on")}
            onClick={() => setDemo((d) => !d)}
          >
            {demo ? "■ Stop" : "▶ Play all"}
          </button>
        </div>
      </div>

      <div className="designer-controls">
        <div className="dfield">
          <span>Presets</span>
          <div className="seg wrap">
            {PRESETS.map((p) => (
              <button key={p.name} type="button" onClick={() => onChange(p.appearance)}>
                {p.name}
              </button>
            ))}
            <button type="button" onClick={() => onChange(randomAppearance())} title="Surprise me">
              🎲 Random
            </button>
          </div>
        </div>
        <Choice label="Body" options={SHAPES} value={value.shape} onPick={(v) => set("shape", v)} />
        <Choice label="Eyes" options={EYES} value={value.eyes} onPick={(v) => set("eyes", v)} />
        <Choice label="Top" options={TOPS} value={value.top} onPick={(v) => set("top", v)} />
        <Choice
          label="Finish"
          options={FINISHES}
          value={value.finish}
          onPick={(v) => set("finish", v)}
        />
        <Swatches
          label="Colour"
          colors={BODY_COLORS}
          value={value.body}
          onPick={(v) => set("body", v)}
        />
        <Swatches
          label="Glow"
          colors={GLOW_COLORS}
          value={value.glow}
          onPick={(v) => set("glow", v)}
          glow
        />
      </div>
    </div>
  );
}

function Choice<T extends string>({
  label,
  options,
  value,
  onPick,
}: {
  label: string;
  options: { id: T; label: string }[];
  value: T;
  onPick: (v: T) => void;
}) {
  return (
    <div className="dfield">
      <span>{label}</span>
      <div className="seg wrap" role="radiogroup" aria-label={label}>
        {options.map((o) => (
          <button
            key={o.id}
            type="button"
            role="radio"
            aria-checked={value === o.id}
            className={cx(value === o.id && "on")}
            onClick={() => onPick(o.id)}
          >
            {o.label}
          </button>
        ))}
      </div>
    </div>
  );
}

function Swatches({
  label,
  colors,
  value,
  onPick,
  glow,
}: {
  label: string;
  colors: string[];
  value: string;
  onPick: (v: string) => void;
  glow?: boolean;
}) {
  return (
    <div className="dfield">
      <span>{label}</span>
      <div className="swatches" role="radiogroup" aria-label={label}>
        {colors.map((c) => (
          <button
            key={c}
            type="button"
            role="radio"
            aria-checked={value.toLowerCase() === c}
            aria-label={c}
            className={cx("swatch", value.toLowerCase() === c && "on", glow && "glow")}
            style={{ "--c": c } as React.CSSProperties}
            onClick={() => onPick(c)}
          />
        ))}
        <label className="swatch custom" title="Custom colour">
          <input
            type="color"
            value={value}
            onChange={(e) => onPick(e.target.value)}
            aria-label={`Custom ${label}`}
          />
        </label>
      </div>
    </div>
  );
}
