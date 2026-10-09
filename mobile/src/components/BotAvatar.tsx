import { useEffect, useState } from "react";
import { Animated, Easing, StyleSheet, View } from "react-native";
import type { Appearance } from "@/lib/api";
import { isDark } from "@/lib/appearance";

/**
 * A bot's face, flat. The web app draws a 3D body in three.js; on a phone list that
 * is too heavy, so this draws the same body colour, glow, eyes, shape and top piece
 * as a few views. While the bot works its eyes breathe; otherwise they blink now and
 * then, so a list of bots still looks alive.
 */
export function BotAvatar({
  look,
  size = 40,
  working = false,
}: {
  look: Appearance;
  size?: number;
  working?: boolean;
}) {
  const [pulse] = useState(() => new Animated.Value(1));
  const [blink] = useState(() => new Animated.Value(1));

  useEffect(() => {
    if (!working) {
      pulse.setValue(1);
      return;
    }
    const loop = Animated.loop(
      Animated.sequence([
        Animated.timing(pulse, {
          toValue: 0.35,
          duration: 650,
          easing: Easing.inOut(Easing.quad),
          useNativeDriver: true,
        }),
        Animated.timing(pulse, {
          toValue: 1,
          duration: 650,
          easing: Easing.inOut(Easing.quad),
          useNativeDriver: true,
        }),
      ]),
    );
    loop.start();
    return () => loop.stop();
  }, [working, pulse]);

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout>;
    const schedule = () => {
      timer = setTimeout(
        () => {
          Animated.sequence([
            Animated.timing(blink, { toValue: 0.1, duration: 80, useNativeDriver: true }),
            Animated.timing(blink, { toValue: 1, duration: 120, useNativeDriver: true }),
          ]).start(schedule);
        },
        2500 + Math.random() * 4000,
      );
    };
    schedule();
    return () => clearTimeout(timer);
  }, [blink]);

  const s = size;
  const bodyRadius =
    look.shape === "orb"
      ? s / 2
      : look.shape === "cube"
        ? s * 0.22
        : look.shape === "tv"
          ? s * 0.18
          : s * 0.38;
  const bodyW = look.shape === "capsule" ? s * 0.84 : s * 0.92;
  const bodyH = look.shape === "pod" ? s * 0.8 : look.shape === "tv" ? s * 0.78 : s * 0.84;
  const plateW = bodyW * (look.shape === "tv" ? 0.78 : 0.74);
  const plateH = bodyH * 0.48;
  const plate = isDark(look.body) ? "#0b0b0d" : "#16171b";
  const eyeH =
    look.eyes === "visor" ? plateH * 0.22 : look.eyes === "dot" ? plateH * 0.26 : plateH * 0.5;
  const eyeW = look.eyes === "visor" ? plateW * 0.64 : look.eyes === "pill" ? plateH * 0.28 : eyeH;
  const eyeRadius = look.eyes === "square" ? eyeH * 0.18 : eyeH / 2;
  const eyes = look.eyes === "visor" ? [0] : [0, 1];

  return (
    <View style={{ width: s, height: s, alignItems: "center", justifyContent: "flex-end" }}>
      <Top look={look} size={s} bodyW={bodyW} bodyH={bodyH} />
      <View
        style={{
          width: bodyW,
          height: bodyH,
          borderRadius: bodyRadius,
          backgroundColor: look.body,
          alignItems: "center",
          justifyContent: "center",
          marginBottom: (s - bodyH) / 2 - (look.top === "none" ? 0 : s * 0.02),
          borderWidth:
            look.finish === "gloss" || look.finish === "pearl" ? StyleSheet.hairlineWidth : 0,
          borderColor: "rgba(255,255,255,0.35)",
        }}
      >
        <View
          style={{
            width: plateW,
            height: plateH,
            borderRadius: Math.min(plateH / 2, bodyRadius),
            backgroundColor: plate,
            flexDirection: "row",
            alignItems: "center",
            justifyContent: "center",
            gap: plateW * 0.16,
          }}
        >
          {eyes.map((i) => (
            <Animated.View
              key={i}
              style={{
                width: eyeW,
                height: eyeH,
                borderRadius: eyeRadius,
                backgroundColor: look.glow,
                opacity: pulse,
                transform: [{ scaleY: blink }],
                shadowColor: look.glow,
                shadowOpacity: 0.9,
                shadowRadius: s * 0.08,
                shadowOffset: { width: 0, height: 0 },
              }}
            />
          ))}
        </View>
      </View>
    </View>
  );
}

function Top({
  look,
  size: s,
  bodyW,
  bodyH,
}: {
  look: Appearance;
  size: number;
  bodyW: number;
  bodyH: number;
}) {
  const top = (s - bodyH) / 2;
  switch (look.top) {
    case "antenna":
      return (
        <View style={{ position: "absolute", top: top - s * 0.1, alignItems: "center" }}>
          <View
            style={{
              width: s * 0.12,
              height: s * 0.12,
              borderRadius: s,
              backgroundColor: look.glow,
            }}
          />
          <View
            style={{ width: Math.max(1.5, s * 0.03), height: s * 0.1, backgroundColor: look.body }}
          />
        </View>
      );
    case "knobs":
      return (
        <View
          style={{
            position: "absolute",
            top: top - s * 0.05,
            flexDirection: "row",
            gap: bodyW * 0.36,
          }}
        >
          {[0, 1].map((i) => (
            <View
              key={i}
              style={{
                width: s * 0.14,
                height: s * 0.1,
                borderRadius: s * 0.04,
                backgroundColor: look.body,
              }}
            />
          ))}
        </View>
      );
    case "ears":
      return (
        <View
          style={{
            position: "absolute",
            top: top - s * 0.08,
            flexDirection: "row",
            gap: bodyW * 0.34,
          }}
        >
          {[0, 1].map((i) => (
            <View
              key={i}
              style={{
                width: s * 0.2,
                height: s * 0.2,
                backgroundColor: look.body,
                borderTopLeftRadius: s * 0.04,
                transform: [{ rotate: "45deg" }],
              }}
            />
          ))}
        </View>
      );
    case "halo":
      return (
        <View
          style={{
            position: "absolute",
            top: top - s * 0.09,
            width: bodyW * 0.6,
            height: s * 0.09,
            borderRadius: s,
            borderWidth: Math.max(1.5, s * 0.035),
            borderColor: look.glow,
          }}
        />
      );
    case "ring":
      return (
        <View
          style={{
            position: "absolute",
            top: top + bodyH * 0.38,
            width: bodyW * 1.08,
            height: bodyH * 0.24,
            borderRadius: s,
            borderWidth: Math.max(1.5, s * 0.03),
            borderColor: look.glow,
            opacity: 0.85,
          }}
        />
      );
    default:
      return null;
  }
}
