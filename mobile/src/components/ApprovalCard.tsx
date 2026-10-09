import { useState } from "react";
import { StyleSheet, Text, View } from "react-native";
import * as Haptics from "expo-haptics";
import { type BotMessage, decide } from "@/lib/api";
import { describeAction } from "@/lib/text";
import { font, radius, usePalette } from "@/lib/theme";
import { Screenshot } from "./Screenshot";
import { Button, Notice } from "./ui";

/**
 * The bot wants to do something consequential and is waiting. The card shows the
 * operation and its real inputs, built by the runtime from the action itself, and the
 * bot's reasoning underneath as what it is: the bot's account of why.
 */
export function ApprovalCard({
  botId,
  message,
  live,
  onDecided,
}: {
  botId: string;
  message: BotMessage;
  live: boolean;
  onDecided: () => void;
}) {
  const p = usePalette();
  const action = message.payload.action;
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const answer = async (d: "once" | "always" | "deny") => {
    setBusy(d);
    setError(null);
    try {
      await decide(botId, message.payload.pending_id ?? "", d);
      void Haptics.notificationAsync(
        d === "deny"
          ? Haptics.NotificationFeedbackType.Warning
          : Haptics.NotificationFeedbackType.Success,
      ).catch(() => undefined);
      onDecided();
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const rows: [string, string | undefined][] = [
    ["URL", action?.url],
    ["Element", action?.element_label],
    ["Text", action?.text === undefined ? undefined : action.secret ? "••••••" : action.text],
    ["On page", action?.page_url],
  ];

  return (
    <View
      style={[
        styles.card,
        {
          backgroundColor: p.surface,
          borderColor: live ? p.accent : p.line,
          opacity: live ? 1 : 0.6,
        },
      ]}
    >
      <View style={styles.titleRow}>
        {live && (
          <View style={[styles.badge, { backgroundColor: p.accent }]}>
            <Text style={[styles.badgeText, { color: p.onAccent }]}>Needs approval</Text>
          </View>
        )}
        {!live && <Text style={[styles.decided, { color: p.textFaint }]}>Decided</Text>}
      </View>
      <Text style={[styles.title, { color: p.text }]}>{describeAction(action)}</Text>
      {rows
        .filter(([, v]) => v)
        .map(([k, v]) => (
          <View key={k} style={styles.row}>
            <Text style={[styles.key, { color: p.textFaint }]}>{k}</Text>
            <Text style={[styles.value, { color: p.textDim }]} numberOfLines={3} selectable>
              {v}
            </Text>
          </View>
        ))}
      {message.content ? (
        <Text style={[styles.reason, { color: p.textDim, borderColor: p.line }]}>
          {message.content}
        </Text>
      ) : null}
      {message.payload.screenshot_id && (
        <Screenshot botId={botId} screenshotId={message.payload.screenshot_id} />
      )}
      {error && <Notice>{error}</Notice>}
      {live && (
        <View style={styles.actions}>
          <Button
            title="Allow once"
            onPress={() => void answer("once")}
            busy={busy === "once"}
            style={styles.flex}
          />
          <View style={styles.actionRow}>
            <Button
              title="Always allow"
              kind="secondary"
              onPress={() => void answer("always")}
              busy={busy === "always"}
              style={styles.flex}
            />
            <Button
              title="Deny"
              kind="danger"
              onPress={() => void answer("deny")}
              busy={busy === "deny"}
              style={styles.flex}
            />
          </View>
        </View>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  card: { borderRadius: radius.lg, borderWidth: 1, padding: 16, gap: 8 },
  titleRow: { flexDirection: "row" },
  badge: { borderRadius: radius.pill, paddingHorizontal: 10, paddingVertical: 4 },
  badgeText: { fontSize: font.tiny, fontWeight: "700", letterSpacing: 0.3 },
  decided: {
    fontSize: font.tiny,
    fontWeight: "600",
    textTransform: "uppercase",
    letterSpacing: 0.6,
  },
  title: { fontSize: font.body, fontWeight: "600", lineHeight: 22 },
  row: { flexDirection: "row", gap: 10 },
  key: { width: 62, fontSize: font.small },
  value: { flex: 1, fontSize: font.small },
  reason: {
    fontSize: font.small,
    lineHeight: 19,
    borderTopWidth: StyleSheet.hairlineWidth,
    paddingTop: 8,
  },
  actions: { gap: 8, marginTop: 6 },
  actionRow: { flexDirection: "row", gap: 8 },
  flex: { flex: 1 },
});
