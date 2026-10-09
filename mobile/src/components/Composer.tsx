import { Ionicons } from "@expo/vector-icons";
import * as ImagePicker from "expo-image-picker";
import { forwardRef, useImperativeHandle, useRef, useState } from "react";
import {
  ActivityIndicator,
  Alert,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";
import { type Attachment, uploadFiles } from "@/lib/api";
import { font, usePalette } from "@/lib/theme";
import { tap } from "./ui";

const MAX_BYTES = 10 * 1024 * 1024;

export interface ComposerHandle {
  setDraft: (text: string) => void;
}

/**
 * Where a message is written. One rounded surface floating over the conversation:
 * the text grows with what is typed, photos attach under it, and the round button
 * sends — or, while the bot is working and nothing is typed, stops it.
 */
export const Composer = forwardRef<
  ComposerHandle,
  {
    botId: string;
    botName: string;
    working: boolean;
    onSend: (text: string, attachments: Attachment[]) => Promise<boolean>;
    onStop: () => void;
  }
>(function Composer({ botId, botName, working, onSend, onStop }, ref) {
  const p = usePalette();
  const [draft, setDraft] = useState("");
  const [attached, setAttached] = useState<(Attachment & { uri?: string })[]>([]);
  const [uploading, setUploading] = useState(0);
  const [sending, setSending] = useState(false);
  const input = useRef<TextInput>(null);

  useImperativeHandle(ref, () => ({
    setDraft: (text: string) => {
      setDraft(text);
      setTimeout(() => input.current?.focus(), 50);
    },
  }));

  const canSend = (draft.trim().length > 0 || attached.length > 0) && uploading === 0 && !sending;
  const showStop = working && !canSend && draft.trim().length === 0;

  const send = async () => {
    if (!canSend) return;
    tap();
    setSending(true);
    const ok = await onSend(draft.trim(), attached);
    setSending(false);
    if (ok) {
      setDraft("");
      setAttached([]);
    }
  };

  const attach = async (source: "library" | "camera") => {
    const options: ImagePicker.ImagePickerOptions = {
      mediaTypes: ["images"],
      base64: true,
      quality: 0.8,
      allowsMultipleSelection: source === "library",
      selectionLimit: 6,
    };
    if (source === "camera") {
      const perm = await ImagePicker.requestCameraPermissionsAsync();
      if (!perm.granted) return;
    }
    const result =
      source === "camera"
        ? await ImagePicker.launchCameraAsync(options)
        : await ImagePicker.launchImageLibraryAsync(options);
    if (result.canceled) return;
    const assets = result.assets.filter((a) => a.base64);
    const tooBig = assets.find((a) => (a.fileSize ?? (a.base64!.length * 3) / 4) > MAX_BYTES);
    if (tooBig) {
      Alert.alert("Too large", `${tooBig.fileName ?? "That photo"} is over 10 MB.`);
      return;
    }
    setUploading((n) => n + assets.length);
    try {
      const stored = await uploadFiles(
        botId,
        assets.map((a, i) => ({
          name: a.fileName ?? `photo-${Date.now()}-${i}.jpg`,
          media_type: a.mimeType ?? "image/jpeg",
          data: a.base64!,
        })),
      );
      setAttached((list) => [
        ...list,
        ...stored.files.map((f, i) => ({ ...f, uri: assets[i]?.uri })),
      ]);
    } catch (cause) {
      Alert.alert("Couldn't attach", (cause as Error).message);
    } finally {
      setUploading((n) => n - assets.length);
    }
  };

  const chooseAttach = () => {
    tap();
    Alert.alert("Attach", undefined, [
      { text: "Photo library", onPress: () => void attach("library") },
      { text: "Take a photo", onPress: () => void attach("camera") },
      { text: "Cancel", style: "cancel" },
    ]);
  };

  return (
    <View style={[styles.shell, { backgroundColor: p.raised, borderColor: p.line }]}>
      {(attached.length > 0 || uploading > 0) && (
        <ScrollView
          horizontal
          showsHorizontalScrollIndicator={false}
          contentContainerStyle={styles.chips}
        >
          {attached.map((a) => (
            <View key={a.id} style={[styles.chip, { backgroundColor: p.pressed }]}>
              <Ionicons name="image-outline" size={14} color={p.textDim} />
              <Text numberOfLines={1} style={[styles.chipText, { color: p.text }]}>
                {a.name}
              </Text>
              <Pressable
                hitSlop={8}
                accessibilityLabel={`Remove ${a.name}`}
                onPress={() => setAttached((list) => list.filter((x) => x.id !== a.id))}
              >
                <Ionicons name="close" size={14} color={p.textDim} />
              </Pressable>
            </View>
          ))}
          {uploading > 0 && (
            <View style={[styles.chip, { backgroundColor: p.pressed }]}>
              <ActivityIndicator size="small" color={p.textDim} />
              <Text style={[styles.chipText, { color: p.textDim }]}>Uploading…</Text>
            </View>
          )}
        </ScrollView>
      )}
      <TextInput
        ref={input}
        value={draft}
        onChangeText={setDraft}
        placeholder={`Ask ${botName} anything`}
        placeholderTextColor={p.textFaint}
        multiline
        style={[styles.input, { color: p.text }]}
        keyboardAppearance={p.scheme}
      />
      <View style={styles.row}>
        <Pressable
          accessibilityRole="button"
          accessibilityLabel="Attach a photo"
          onPress={chooseAttach}
          style={({ pressed }) => [
            styles.round,
            { borderColor: p.lineStrong },
            pressed && { backgroundColor: p.pressed },
          ]}
        >
          <Ionicons name="add" size={22} color={p.text} />
        </Pressable>
        <View style={{ flex: 1 }} />
        {showStop ? (
          <Pressable
            accessibilityRole="button"
            accessibilityLabel={`Stop ${botName}`}
            onPress={() => {
              tap();
              onStop();
            }}
            style={({ pressed }) => [
              styles.send,
              { backgroundColor: p.accent, opacity: pressed ? 0.7 : 1 },
            ]}
          >
            <View style={[styles.stopSquare, { backgroundColor: p.onAccent }]} />
          </Pressable>
        ) : (
          <Pressable
            accessibilityRole="button"
            accessibilityLabel="Send"
            disabled={!canSend}
            onPress={() => void send()}
            style={({ pressed }) => [
              styles.send,
              { backgroundColor: canSend ? p.accent : p.pressed, opacity: pressed ? 0.7 : 1 },
            ]}
          >
            {sending ? (
              <ActivityIndicator color={p.onAccent} size="small" />
            ) : (
              <Ionicons name="arrow-up" size={20} color={canSend ? p.onAccent : p.textFaint} />
            )}
          </Pressable>
        )}
      </View>
    </View>
  );
});

const styles = StyleSheet.create({
  shell: {
    borderRadius: 28,
    borderWidth: StyleSheet.hairlineWidth,
    paddingTop: 12,
    paddingBottom: 8,
    paddingHorizontal: 8,
  },
  input: {
    fontSize: font.body,
    lineHeight: 22,
    maxHeight: 160,
    minHeight: 26,
    paddingHorizontal: 10,
    paddingTop: 0,
    paddingBottom: 6,
  },
  row: { flexDirection: "row", alignItems: "center", gap: 8 },
  round: {
    width: 38,
    height: 38,
    borderRadius: 19,
    borderWidth: 1,
    alignItems: "center",
    justifyContent: "center",
  },
  send: { width: 38, height: 38, borderRadius: 19, alignItems: "center", justifyContent: "center" },
  stopSquare: { width: 12, height: 12, borderRadius: 2 },
  chips: { gap: 6, paddingHorizontal: 4, paddingBottom: 10 },
  chip: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    paddingHorizontal: 10,
    paddingVertical: 6,
    borderRadius: 14,
    maxWidth: 200,
  },
  chipText: { fontSize: font.small, flexShrink: 1 },
});
