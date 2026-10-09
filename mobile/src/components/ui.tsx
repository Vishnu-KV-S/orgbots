import { Ionicons } from "@expo/vector-icons";
import * as Haptics from "expo-haptics";
import type { ComponentProps, ReactNode } from "react";
import { ActivityIndicator, Pressable, StyleSheet, Text, type ViewStyle } from "react-native";
import { font, radius, usePalette } from "@/lib/theme";

export type IconName = ComponentProps<typeof Ionicons>["name"];

export function tap() {
  void Haptics.selectionAsync().catch(() => undefined);
}

export function Button({
  title,
  onPress,
  kind = "primary",
  icon,
  busy,
  disabled,
  style,
}: {
  title: string;
  onPress: () => void;
  kind?: "primary" | "secondary" | "ghost" | "danger";
  icon?: IconName;
  busy?: boolean;
  disabled?: boolean;
  style?: ViewStyle;
}) {
  const p = usePalette();
  const bg = kind === "primary" ? p.accent : kind === "ghost" ? "transparent" : p.raised;
  const fg = kind === "primary" ? p.onAccent : kind === "danger" ? "#ff6b5e" : p.text;
  return (
    <Pressable
      accessibilityRole="button"
      disabled={disabled || busy}
      onPress={() => {
        tap();
        onPress();
      }}
      style={({ pressed }) => [
        styles.button,
        { backgroundColor: bg, opacity: disabled ? 0.4 : pressed ? 0.75 : 1 },
        kind === "ghost" && { borderWidth: 1, borderColor: p.lineStrong },
        style,
      ]}
    >
      {busy ? (
        <ActivityIndicator color={fg} />
      ) : (
        <>
          {icon && <Ionicons name={icon} size={18} color={fg} />}
          <Text style={[styles.buttonText, { color: fg }]}>{title}</Text>
        </>
      )}
    </Pressable>
  );
}

export function IconButton({
  name,
  onPress,
  label,
  size = 22,
  color,
  filled,
}: {
  name: IconName;
  onPress: () => void;
  label: string;
  size?: number;
  color?: string;
  filled?: boolean;
}) {
  const p = usePalette();
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={label}
      hitSlop={8}
      onPress={() => {
        tap();
        onPress();
      }}
      style={({ pressed }) => [
        styles.icon,
        filled && { backgroundColor: p.raised },
        pressed && { backgroundColor: p.pressed },
      ]}
    >
      <Ionicons name={name} size={size} color={color ?? p.text} />
    </Pressable>
  );
}

export function Notice({
  children,
  tone = "error",
}: {
  children: ReactNode;
  tone?: "error" | "info";
}) {
  const p = usePalette();
  return (
    <Text
      style={[
        styles.notice,
        { color: tone === "error" ? "#ff6b5e" : p.textDim, backgroundColor: p.raised },
      ]}
    >
      {children}
    </Text>
  );
}

const styles = StyleSheet.create({
  button: {
    minHeight: 50,
    paddingHorizontal: 20,
    borderRadius: radius.pill,
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    gap: 8,
  },
  buttonText: { fontSize: font.body, fontWeight: "600" },
  icon: {
    width: 40,
    height: 40,
    borderRadius: 20,
    alignItems: "center",
    justifyContent: "center",
  },
  notice: {
    fontSize: font.small,
    lineHeight: 19,
    padding: 12,
    borderRadius: radius.md,
    overflow: "hidden",
  },
});
