import { Ionicons } from "@expo/vector-icons";
import { useEffect, useState } from "react";
import { ActivityIndicator, Animated, Pressable, StyleSheet, Text, View } from "react-native";
import type { BotMessage } from "@/lib/api";
import { ACTION_ICON, describeAction } from "@/lib/text";
import { font, radius, usePalette } from "@/lib/theme";

/**
 * Consecutive steps folded into one line: "Worked · 6 steps · 1 retried". While the
 * bot is on it the line shows the latest step and the last few steps stay open, so
 * you can watch it work; afterwards it collapses and opens on tap.
 */
export function WorkBlock({ steps, live }: { steps: BotMessage[]; live: boolean }) {
  const p = usePalette();
  const [open, setOpen] = useState(false);
  const expanded = open || live;
  const latest = steps[steps.length - 1];
  const failures = steps.filter((s) => s.payload.ok === false).length;
  const [shimmer] = useState(() => new Animated.Value(0.5));

  useEffect(() => {
    if (!live) return;
    const loop = Animated.loop(
      Animated.sequence([
        Animated.timing(shimmer, { toValue: 1, duration: 700, useNativeDriver: true }),
        Animated.timing(shimmer, { toValue: 0.5, duration: 700, useNativeDriver: true }),
      ]),
    );
    loop.start();
    return () => loop.stop();
  }, [live, shimmer]);

  const summary = `${live ? describeAction(latest.payload.action) || "Working" : "Worked"} · ${steps.length} step${
    steps.length === 1 ? "" : "s"
  }${failures > 0 ? ` · ${failures} retried` : ""}`;

  return (
    <View style={[styles.block, { borderColor: p.line, backgroundColor: p.surface }]}>
      <Pressable
        accessibilityRole="button"
        accessibilityState={{ expanded }}
        onPress={() => setOpen((o) => !o)}
        style={styles.head}
      >
        {live ? (
          <ActivityIndicator size="small" color={p.textDim} />
        ) : (
          <Ionicons name="checkmark" size={16} color={p.textDim} />
        )}
        <Animated.Text
          numberOfLines={1}
          style={[styles.summary, { color: p.textDim, opacity: live ? shimmer : 1 }]}
        >
          {summary}
        </Animated.Text>
        <Ionicons
          name={expanded ? "chevron-down" : "chevron-forward"}
          size={14}
          color={p.textFaint}
        />
      </Pressable>
      {expanded && (
        <View style={styles.steps}>
          {(live ? steps.slice(-6) : steps).map((s) => {
            const action = s.payload.action;
            const failed = s.payload.ok === false;
            return (
              <View key={s.id} style={styles.step}>
                <Ionicons
                  name={ACTION_ICON[action?.type ?? ""] ?? "chevron-forward"}
                  size={15}
                  color={p.textFaint}
                  style={{ marginTop: 2 }}
                />
                <View style={{ flex: 1 }}>
                  <Text
                    numberOfLines={2}
                    style={[styles.what, { color: failed ? p.textFaint : p.text }]}
                  >
                    {describeAction(action) || s.content}
                  </Text>
                  {failed && s.payload.error ? (
                    <Text numberOfLines={2} style={[styles.why, { color: p.textFaint }]}>
                      {s.payload.error}
                    </Text>
                  ) : s.content && action?.type !== "plan" ? (
                    <Text numberOfLines={3} style={[styles.why, { color: p.textFaint }]}>
                      {s.content}
                    </Text>
                  ) : null}
                </View>
              </View>
            );
          })}
        </View>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  block: { borderRadius: radius.md, borderWidth: StyleSheet.hairlineWidth, overflow: "hidden" },
  head: {
    flexDirection: "row",
    alignItems: "center",
    gap: 10,
    paddingHorizontal: 14,
    paddingVertical: 11,
  },
  summary: { flex: 1, fontSize: font.small },
  steps: { paddingHorizontal: 14, paddingBottom: 12, gap: 10 },
  step: { flexDirection: "row", gap: 10 },
  what: { fontSize: font.small, lineHeight: 18 },
  why: { fontSize: font.tiny + 1, lineHeight: 16, marginTop: 2 },
});
