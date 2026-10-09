import { Ionicons } from "@expo/vector-icons";
import * as Clipboard from "expo-clipboard";
import * as Haptics from "expo-haptics";
import { useState } from "react";
import { ActionSheetIOS, Alert, Platform, Pressable, StyleSheet, Text, View } from "react-native";
import { type Appearance, type BotMessage, reactToMessage } from "@/lib/api";
import { font, radius, usePalette } from "@/lib/theme";
import { ApprovalCard } from "./ApprovalCard";
import { BotAvatar } from "./BotAvatar";
import { CredentialCard } from "./CredentialCard";
import { RichText } from "./RichText";
import { Screenshot } from "./Screenshot";
import { WorkBlock } from "./WorkBlock";

export type Item =
  | { kind: "message"; id: string; message: BotMessage }
  | { kind: "work"; id: string; steps: BotMessage[] };

/** Consecutive activity rows fold into one "worked" block, as on the web. */
export function group(messages: BotMessage[]): Item[] {
  const items: Item[] = [];
  for (const m of messages) {
    if (m.role === "activity") {
      const last = items[items.length - 1];
      if (last?.kind === "work") last.steps.push(m);
      else items.push({ kind: "work", id: m.id, steps: [m] });
    } else {
      items.push({ kind: "message", id: m.id, message: m });
    }
  }
  return items;
}

const QUICK = ["👍", "👎", "❤️", "🎉"];

export function ItemView({
  item,
  botId,
  botName,
  look,
  live,
  pending,
  asking,
  onChanged,
}: {
  item: Item;
  botId: string;
  botName: string;
  look: Appearance;
  /** This is the last item and the bot is still working on it. */
  live: boolean;
  pending: Set<string>;
  asking: Set<string>;
  onChanged: () => void;
}) {
  const p = usePalette();
  const [reactions, setReactions] = useState<string[] | null>(null);

  if (item.kind === "work") return <WorkBlock steps={item.steps} live={live} />;
  const m = item.message;
  const mine = reactions ?? m.reactions ?? [];

  const react = async (emoji: string) => {
    const on = !mine.includes(emoji);
    try {
      const result = await reactToMessage(botId, m.id, emoji, on);
      setReactions(result.reactions);
    } catch {
      /* a reaction that did not save just does not show */
    }
  };

  const menu = () => {
    void Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Medium).catch(() => undefined);
    const options = ["Copy text", ...QUICK.map((e) => `React ${e}`), "Cancel"];
    const pick = (i: number) => {
      if (i === 0) void Clipboard.setStringAsync(m.content);
      else if (i <= QUICK.length) void react(QUICK[i - 1]);
    };
    if (Platform.OS === "ios") {
      ActionSheetIOS.showActionSheetWithOptions(
        { options, cancelButtonIndex: options.length - 1 },
        pick,
      );
    } else {
      Alert.alert("Message", undefined, [
        { text: "Copy text", onPress: () => pick(0) },
        { text: "👍", onPress: () => pick(1) },
        { text: "👎", onPress: () => pick(2) },
      ]);
    }
  };

  switch (m.role) {
    case "user": {
      const attachments = m.payload.attachments ?? [];
      return (
        <View style={styles.userWrap}>
          {m.payload.from?.name ? (
            <Text style={[styles.from, { color: p.textFaint }]}>{m.payload.from.name}</Text>
          ) : m.payload.routine ? (
            <Text style={[styles.from, { color: p.textFaint }]}>Routine · {m.payload.routine}</Text>
          ) : null}
          {attachments.length > 0 && (
            <View style={styles.attachRow}>
              {attachments.map((a) => (
                <View key={a.id} style={[styles.attach, { backgroundColor: p.bubble }]}>
                  <Ionicons
                    name={a.media_type.startsWith("image/") ? "image-outline" : "document-outline"}
                    size={14}
                    color={p.textDim}
                  />
                  <Text numberOfLines={1} style={[styles.attachText, { color: p.text }]}>
                    {a.name}
                  </Text>
                </View>
              ))}
            </View>
          )}
          {m.content ? (
            <Pressable onLongPress={menu} style={[styles.bubble, { backgroundColor: p.bubble }]}>
              <Text style={[styles.userText, { color: p.text }]} selectable>
                {m.content}
              </Text>
            </Pressable>
          ) : null}
        </View>
      );
    }
    case "bot":
      return (
        <Pressable onLongPress={menu} style={styles.botWrap}>
          <View style={styles.botHead}>
            <BotAvatar look={look} size={24} />
            <Text style={[styles.botName, { color: p.textDim }]}>{botName}</Text>
          </View>
          <RichText text={m.content} />
          {m.payload.screenshot_id && (
            <Screenshot botId={botId} screenshotId={m.payload.screenshot_id} />
          )}
          {mine.length > 0 && (
            <View style={styles.reactRow}>
              {mine.map((e) => (
                <Pressable
                  key={e}
                  onPress={() => void react(e)}
                  style={[styles.react, { backgroundColor: p.raised }]}
                >
                  <Text>{e}</Text>
                </Pressable>
              ))}
            </View>
          )}
        </Pressable>
      );
    case "approval":
      return (
        <ApprovalCard
          botId={botId}
          message={m}
          live={pending.has(m.payload.pending_id ?? "")}
          onDecided={onChanged}
        />
      );
    case "credentials":
      return (
        <CredentialCard
          botId={botId}
          botName={botName}
          message={m}
          live={asking.has(m.payload.credential_request_id ?? "")}
          onDone={onChanged}
        />
      );
    case "error":
      return (
        <View style={[styles.error, { backgroundColor: p.surface, borderColor: p.lineStrong }]}>
          <Ionicons name="alert-circle-outline" size={18} color={p.text} />
          <Text style={[styles.errorText, { color: p.text }]}>{m.content}</Text>
        </View>
      );
    default:
      return m.content ? (
        <Text style={[styles.system, { color: p.textFaint }]}>{m.content}</Text>
      ) : null;
  }
}

const styles = StyleSheet.create({
  userWrap: { alignItems: "flex-end", gap: 6, paddingLeft: 48 },
  from: { fontSize: font.tiny, marginRight: 8 },
  bubble: {
    borderRadius: radius.lg,
    borderBottomRightRadius: 6,
    paddingHorizontal: 16,
    paddingVertical: 11,
  },
  userText: { fontSize: font.body, lineHeight: 23 },
  attachRow: { flexDirection: "row", flexWrap: "wrap", gap: 6, justifyContent: "flex-end" },
  attach: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    borderRadius: radius.md,
    paddingHorizontal: 10,
    paddingVertical: 8,
    maxWidth: 220,
  },
  attachText: { fontSize: font.small, flexShrink: 1 },
  botWrap: { gap: 10 },
  botHead: { flexDirection: "row", alignItems: "center", gap: 8 },
  botName: { fontSize: font.small, fontWeight: "600" },
  reactRow: { flexDirection: "row", gap: 6 },
  react: { borderRadius: radius.pill, paddingHorizontal: 10, paddingVertical: 4 },
  error: {
    flexDirection: "row",
    gap: 10,
    borderRadius: radius.md,
    borderWidth: 1,
    padding: 12,
    alignItems: "flex-start",
  },
  errorText: { flex: 1, fontSize: font.small, lineHeight: 19 },
  system: { fontSize: font.small, textAlign: "center", paddingHorizontal: 24 },
});
