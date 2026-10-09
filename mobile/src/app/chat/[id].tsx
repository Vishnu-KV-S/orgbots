import { Ionicons } from "@expo/vector-icons";
import { router, useLocalSearchParams } from "expo-router";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  ActivityIndicator,
  FlatList,
  KeyboardAvoidingView,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from "react-native";
import { SafeAreaView, useSafeAreaInsets } from "react-native-safe-area-context";
import { BotAvatar } from "@/components/BotAvatar";
import { Composer, type ComposerHandle } from "@/components/Composer";
import { ItemView, group } from "@/components/Messages";
import { IconButton, Notice, tap } from "@/components/ui";
import { useConversation } from "@/hooks/useConversation";
import { type Attachment, type Bot, getBot, markRead, sendMessage, stopBot } from "@/lib/api";
import { appearanceFor } from "@/lib/appearance";
import { STARTERS } from "@/lib/templates";
import { font, radius, usePalette } from "@/lib/theme";

/**
 * Talking to one bot. Replies read full-width like a document, your messages sit in
 * bubbles, steps fold into a live "Working" line, and anything the bot needs from you
 * — an approval, a sign-in — appears as a card right in the conversation.
 */
export default function Chat() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const p = usePalette();
  const insets = useSafeAreaInsets();
  const [bot, setBot] = useState<Bot | null>(null);
  const [botError, setBotError] = useState<string | null>(null);
  const [sendError, setSendError] = useState<string | null>(null);
  const convo = useConversation(id);
  const composer = useRef<ComposerHandle>(null);

  useEffect(() => {
    getBot(id).then(setBot, (e: Error) => setBotError(e.message));
  }, [id]);

  // Reading the conversation is reading it: clear the unread dot as messages land.
  const count = convo.messages.length;
  useEffect(() => {
    if (convo.loaded) void markRead(id).catch(() => undefined);
  }, [id, count, convo.loaded]);

  const items = useMemo(() => group(convo.messages), [convo.messages]);
  const reversed = useMemo(() => [...items].reverse(), [items]);
  const look = useMemo(() => appearanceFor(bot ?? { id }), [bot, id]);
  const name = bot?.name ?? "Bot";
  const waiting = convo.pending.size > 0 || convo.asking.size > 0;

  const send = async (text: string, attachments: Attachment[]) => {
    setSendError(null);
    try {
      const sent = await sendMessage(
        id,
        text,
        attachments.map((a) => a.id),
      );
      if (!sent.admitted && sent.refusal_reason) setSendError(sent.refusal_reason);
      convo.poke();
      return true;
    } catch (cause) {
      setSendError((cause as Error).message);
      return false;
    }
  };

  const status = waiting ? "Needs you" : convo.working ? "Working…" : bot?.label || "Online";

  const starters = useMemo(() => {
    const duties = (bot?.brief?.duties ?? [])
      .slice(0, 3)
      .map((d) => ({ title: d, text: `${d}: ` }));
    return [...duties, ...STARTERS].slice(0, 5);
  }, [bot]);

  return (
    <SafeAreaView style={{ flex: 1, backgroundColor: p.bg }} edges={["top"]}>
      <View style={[styles.header, { borderColor: p.line }]}>
        <IconButton name="chevron-back" label="Back" onPress={() => router.back()} />
        <View style={styles.headerMid}>
          <BotAvatar look={look} size={30} working={convo.working} />
          <View style={{ flexShrink: 1 }}>
            <Text numberOfLines={1} style={[styles.title, { color: p.text }]}>
              {name}
            </Text>
            <Text
              numberOfLines={1}
              style={[styles.status, { color: waiting ? p.text : p.textFaint }]}
            >
              {status}
            </Text>
          </View>
        </View>
        <IconButton
          name="desktop-outline"
          label={`Watch ${name}'s screen`}
          onPress={() => router.push({ pathname: "/screen/[id]", params: { id, name } })}
        />
      </View>

      <KeyboardAvoidingView style={{ flex: 1 }} behavior="padding" keyboardVerticalOffset={0}>
        {!convo.loaded ? (
          <View style={styles.center}>
            {convo.error || botError ? (
              <View style={{ padding: 24 }}>
                <Notice>{convo.error ?? botError}</Notice>
              </View>
            ) : (
              <ActivityIndicator color={p.textDim} />
            )}
          </View>
        ) : items.length === 0 ? (
          <ScrollView contentContainerStyle={styles.welcome} keyboardShouldPersistTaps="handled">
            <BotAvatar look={look} size={96} working={convo.working} />
            <Text style={[styles.hello, { color: p.text }]}>What can I do for you?</Text>
            {bot?.brief?.mission ? (
              <Text style={[styles.mission, { color: p.textDim }]}>{bot.brief.mission}</Text>
            ) : bot?.description ? (
              <Text style={[styles.mission, { color: p.textDim }]}>{bot.description}</Text>
            ) : null}
          </ScrollView>
        ) : (
          <FlatList
            inverted
            data={reversed}
            keyExtractor={(item) => item.id}
            keyboardDismissMode="interactive"
            keyboardShouldPersistTaps="handled"
            contentContainerStyle={styles.list}
            ItemSeparatorComponent={() => <View style={{ height: 18 }} />}
            ListHeaderComponent={
              convo.working && !waiting && items[items.length - 1]?.kind !== "work" ? (
                <Thinking name={name} />
              ) : null
            }
            renderItem={({ item, index }) => (
              <ItemView
                item={item}
                botId={id}
                botName={name}
                look={look}
                live={index === 0 && convo.working}
                pending={convo.pending}
                asking={convo.asking}
                onChanged={convo.poke}
              />
            )}
          />
        )}

        <View style={[styles.bottom, { paddingBottom: Math.max(insets.bottom, 10) }]}>
          {sendError && (
            <Pressable onPress={() => setSendError(null)}>
              <Notice>{sendError}</Notice>
            </Pressable>
          )}
          {convo.loaded && items.length === 0 && (
            <ScrollView
              horizontal
              showsHorizontalScrollIndicator={false}
              keyboardShouldPersistTaps="handled"
              contentContainerStyle={styles.starters}
            >
              {starters.map((s) => (
                <Pressable
                  key={s.title}
                  onPress={() => {
                    tap();
                    composer.current?.setDraft(s.text);
                  }}
                  style={({ pressed }) => [
                    styles.starter,
                    {
                      borderColor: p.lineStrong,
                      backgroundColor: pressed ? p.raised : "transparent",
                    },
                  ]}
                >
                  <Text numberOfLines={1} style={[styles.starterText, { color: p.text }]}>
                    {s.title}
                  </Text>
                </Pressable>
              ))}
            </ScrollView>
          )}
          <Composer
            ref={composer}
            botId={id}
            botName={name}
            working={convo.working}
            onSend={send}
            onStop={() => void stopBot(id).then(convo.poke, (e: Error) => setSendError(e.message))}
          />
        </View>
      </KeyboardAvoidingView>
    </SafeAreaView>
  );
}

function Thinking({ name }: { name: string }) {
  const p = usePalette();
  return (
    <View style={styles.thinking}>
      <ActivityIndicator size="small" color={p.textFaint} />
      <Text style={[styles.thinkingText, { color: p.textFaint }]}>{name} is thinking…</Text>
      <Ionicons name="sparkles-outline" size={14} color={p.textFaint} />
    </View>
  );
}

const styles = StyleSheet.create({
  header: {
    flexDirection: "row",
    alignItems: "center",
    paddingHorizontal: 6,
    paddingVertical: 4,
    borderBottomWidth: StyleSheet.hairlineWidth,
  },
  headerMid: {
    flex: 1,
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    gap: 10,
  },
  title: { fontSize: font.heading, fontWeight: "700" },
  status: { fontSize: font.tiny + 1 },
  center: { flex: 1, alignItems: "center", justifyContent: "center" },
  list: { paddingHorizontal: 18, paddingVertical: 20 },
  welcome: { flexGrow: 1, alignItems: "center", justifyContent: "center", padding: 32, gap: 14 },
  hello: {
    fontSize: font.title,
    fontWeight: "700",
    textAlign: "center",
    letterSpacing: -0.5,
    marginTop: 8,
  },
  mission: { fontSize: font.body, lineHeight: 23, textAlign: "center" },
  bottom: { paddingHorizontal: 12, paddingTop: 6, gap: 8 },
  starters: { gap: 8, paddingHorizontal: 2 },
  starter: {
    borderWidth: 1,
    borderRadius: radius.pill,
    paddingHorizontal: 16,
    paddingVertical: 10,
    maxWidth: 260,
  },
  starterText: { fontSize: font.small + 1 },
  thinking: { flexDirection: "row", alignItems: "center", gap: 8, paddingTop: 18 },
  thinkingText: { fontSize: font.small },
});
