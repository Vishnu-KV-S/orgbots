import { Ionicons } from "@expo/vector-icons";
import { Image } from "expo-image";
import { useState } from "react";
import { ActivityIndicator, Modal, Pressable, StyleSheet, Text, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { useAuthedImage } from "@/hooks/useAuthedImage";
import { screenshotUrl, VIEWPORT } from "@/lib/api";
import { font, radius, usePalette } from "@/lib/theme";

/**
 * A picture of the bot's screen, kept by the run at a moment that mattered. Masked by
 * the server like everything a bot sees. Tap for full screen.
 */
export function Screenshot({ botId, screenshotId }: { botId: string; screenshotId: string }) {
  const p = usePalette();
  const insets = useSafeAreaInsets();
  const { uri, gone } = useAuthedImage(screenshotUrl(botId, screenshotId));
  const [open, setOpen] = useState(false);

  if (gone) {
    return <Text style={[styles.gone, { color: p.textFaint }]}>Screenshot no longer kept</Text>;
  }
  return (
    <>
      <Pressable
        accessibilityRole="imagebutton"
        accessibilityLabel="Screenshot of the bot's screen"
        onPress={() => uri && setOpen(true)}
        style={[styles.thumb, { backgroundColor: p.raised, borderColor: p.line }]}
      >
        {uri ? (
          <Image source={{ uri }} style={StyleSheet.absoluteFill} contentFit="cover" />
        ) : (
          <ActivityIndicator color={p.textFaint} />
        )}
      </Pressable>
      <Modal visible={open} transparent animationType="fade" onRequestClose={() => setOpen(false)}>
        <Pressable style={styles.backdrop} onPress={() => setOpen(false)}>
          {uri && <Image source={{ uri }} style={styles.full} contentFit="contain" />}
          <View style={[styles.close, { top: insets.top + 8 }]}>
            <Ionicons name="close" size={26} color="#fff" />
          </View>
        </Pressable>
      </Modal>
    </>
  );
}

const styles = StyleSheet.create({
  thumb: {
    width: "100%",
    aspectRatio: VIEWPORT.width / VIEWPORT.height,
    borderRadius: radius.md,
    borderWidth: StyleSheet.hairlineWidth,
    overflow: "hidden",
    alignItems: "center",
    justifyContent: "center",
    marginTop: 8,
  },
  gone: { fontSize: font.small, marginTop: 6 },
  backdrop: { flex: 1, backgroundColor: "rgba(0,0,0,0.95)", justifyContent: "center" },
  full: { width: "100%", height: "80%" },
  close: { position: "absolute", right: 16 },
});
