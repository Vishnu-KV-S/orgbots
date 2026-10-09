import { Ionicons } from "@expo/vector-icons";
import { router } from "expo-router";
import { useMemo, useState } from "react";
import {
  ActionSheetIOS,
  ActivityIndicator,
  Alert,
  FlatList,
  Platform,
  Pressable,
  RefreshControl,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";
import { SafeAreaView, useSafeAreaInsets } from "react-native-safe-area-context";
import { BotAvatar } from "@/components/BotAvatar";
import { Button, IconButton, Notice, tap } from "@/components/ui";
import { useBots } from "@/hooks/useBots";
import { type Bot, deleteBot, updateBot } from "@/lib/api";
import { PRESETS, appearanceFor } from "@/lib/appearance";
import { useSession } from "@/lib/session";
import { preview, timeAgo } from "@/lib/text";
import { font, radius, usePalette } from "@/lib/theme";

/**
 * Home: every bot, most recent first, with the ones waiting on you on top. It is the
 * phone's version of the web app's sidebar — open one to talk to it.
 */
export default function Home() {
  const p = usePalette();
  const insets = useSafeAreaInsets();
  const { me, status, refresh: reconnect } = useSession();
  const { bots, error, refreshing, refresh, setBots } = useBots();
  const [query, setQuery] = useState("");

  const names = useMemo(() => new Map((bots ?? []).map((b) => [b.id, b.name])), [bots]);

  const sections = useMemo(() => {
    const q = query.trim().toLowerCase();
    const visible = (bots ?? []).filter(
      (b) =>
        !b.hidden &&
        (!q ||
          b.name.toLowerCase().includes(q) ||
          b.label.toLowerCase().includes(q) ||
          (b.last_message?.content ?? "").toLowerCase().includes(q)),
    );
    const recent = (b: Bot) => new Date(b.last_message?.created_at ?? b.updated_at).getTime();
    visible.sort((a, b) => {
      if (a.needs_attention !== b.needs_attention) return a.needs_attention ? -1 : 1;
      if (a.pinned !== b.pinned) return a.pinned ? -1 : 1;
      return recent(b) - recent(a);
    });
    return visible;
  }, [bots, query]);

  const options = (bot: Bot) => {
    const pin = bot.pinned ? "Unpin" : "Pin to top";
    const run = (i: number) => {
      if (i === 0) {
        setBots(
          (list) => list?.map((b) => (b.id === bot.id ? { ...b, pinned: !b.pinned } : b)) ?? list,
        );
        void updateBot(bot.id, { pinned: !bot.pinned }).catch((e: Error) =>
          Alert.alert("Couldn't change", e.message),
        );
      } else if (i === 1) {
        Alert.alert(
          `Delete ${bot.name}?`,
          "Its conversation, memory and routines go with it. Helpers move up a level.",
          [
            { text: "Cancel", style: "cancel" },
            {
              text: "Delete",
              style: "destructive",
              onPress: () =>
                void deleteBot(bot.id)
                  .then(refresh)
                  .catch((e: Error) => Alert.alert("Couldn't delete", e.message)),
            },
          ],
        );
      }
    };
    if (Platform.OS === "ios") {
      ActionSheetIOS.showActionSheetWithOptions(
        {
          title: bot.name,
          options: [pin, "Delete", "Cancel"],
          destructiveButtonIndex: 1,
          cancelButtonIndex: 2,
        },
        run,
      );
    } else {
      Alert.alert(bot.name, undefined, [
        { text: pin, onPress: () => run(0) },
        { text: "Delete", style: "destructive", onPress: () => run(1) },
        { text: "Cancel", style: "cancel" },
      ]);
    }
  };

  return (
    <SafeAreaView style={{ flex: 1, backgroundColor: p.bg }} edges={["top"]}>
      <View style={styles.header}>
        <Pressable
          accessibilityRole="button"
          accessibilityLabel="Settings"
          onPress={() => {
            tap();
            router.push("/settings");
          }}
          style={[styles.me, { backgroundColor: p.raised, borderColor: p.line }]}
        >
          {me ? (
            <Text style={[styles.meText, { color: p.text }]}>
              {(me.name || me.email).slice(0, 1).toUpperCase()}
            </Text>
          ) : (
            <Ionicons name="settings-outline" size={18} color={p.text} />
          )}
        </Pressable>
        <Text style={[styles.brand, { color: p.text }]}>Orgbots</Text>
        <IconButton name="create-outline" label="New bot" onPress={() => router.push("/new")} />
      </View>

      <View style={[styles.search, { backgroundColor: p.raised }]}>
        <Ionicons name="search" size={17} color={p.textFaint} />
        <TextInput
          value={query}
          onChangeText={setQuery}
          placeholder="Search bots and messages"
          placeholderTextColor={p.textFaint}
          style={[styles.searchInput, { color: p.text }]}
          keyboardAppearance={p.scheme}
          clearButtonMode="while-editing"
          returnKeyType="search"
        />
      </View>

      {status === "offline" && (
        <View style={styles.pad}>
          <Notice>{"Can’t reach your server right now."}</Notice>
          <Button
            title="Try again"
            kind="secondary"
            onPress={() => void reconnect().then(refresh)}
          />
        </View>
      )}
      {error && status !== "offline" && bots && (
        <View style={styles.pad}>
          <Notice>{error}</Notice>
        </View>
      )}

      {bots === null ? (
        error ? (
          <View style={styles.pad}>
            <Notice>{error}</Notice>
            <Button title="Try again" kind="secondary" onPress={() => void refresh()} />
          </View>
        ) : (
          <ActivityIndicator style={{ marginTop: 48 }} color={p.textDim} />
        )
      ) : (
        <FlatList
          data={sections}
          keyExtractor={(b) => b.id}
          contentContainerStyle={{ paddingBottom: insets.bottom + 100, flexGrow: 1 }}
          keyboardDismissMode="on-drag"
          refreshControl={
            <RefreshControl
              refreshing={refreshing}
              onRefresh={() => void refresh()}
              tintColor={p.textDim}
            />
          }
          ListEmptyComponent={
            query ? (
              <Text
                style={[styles.empty, { color: p.textFaint }]}
              >{`Nothing matches “${query}”.`}</Text>
            ) : (
              <EmptyState />
            )
          }
          renderItem={({ item }) => (
            <BotRow
              bot={item}
              parent={item.parent_bot_id ? names.get(item.parent_bot_id) : undefined}
              onPress={() => {
                tap();
                router.push({ pathname: "/chat/[id]", params: { id: item.id } });
              }}
              onLongPress={() => options(item)}
            />
          )}
        />
      )}

      {bots && bots.length > 0 && (
        <View style={[styles.fabWrap, { bottom: insets.bottom + 16 }]} pointerEvents="box-none">
          <Pressable
            accessibilityRole="button"
            onPress={() => {
              tap();
              router.push("/new");
            }}
            style={({ pressed }) => [
              styles.fab,
              { backgroundColor: p.accent, opacity: pressed ? 0.85 : 1 },
            ]}
          >
            <Ionicons name="add" size={20} color={p.onAccent} />
            <Text style={[styles.fabText, { color: p.onAccent }]}>New bot</Text>
          </Pressable>
        </View>
      )}
    </SafeAreaView>
  );
}

function BotRow({
  bot,
  parent,
  onPress,
  onLongPress,
}: {
  bot: Bot;
  parent?: string;
  onPress: () => void;
  onLongPress: () => void;
}) {
  const p = usePalette();
  const last = bot.last_message;
  const subtitle = bot.needs_attention
    ? "Waiting for you"
    : bot.working
      ? "Working…"
      : last
        ? `${last.role === "user" ? "You: " : ""}${preview(last.content)}`
        : bot.label || bot.description || "Say hello";
  return (
    <Pressable
      onPress={onPress}
      onLongPress={onLongPress}
      style={({ pressed }) => [styles.row, pressed && { backgroundColor: p.raised }]}
    >
      <BotAvatar look={appearanceFor(bot)} size={50} working={bot.working} />
      <View style={styles.rowBody}>
        <View style={styles.rowTop}>
          <Text
            numberOfLines={1}
            style={[styles.name, { color: p.text }, bot.unread && styles.bold]}
          >
            {bot.name}
          </Text>
          {bot.pinned && <Ionicons name="pin" size={12} color={p.textFaint} />}
          {bot.visibility === "team" && <Ionicons name="people" size={13} color={p.textFaint} />}
          <View style={{ flex: 1 }} />
          {last && (
            <Text style={[styles.time, { color: p.textFaint }]}>{timeAgo(last.created_at)}</Text>
          )}
        </View>
        <View style={styles.rowTop}>
          <Text
            numberOfLines={1}
            style={[
              styles.preview,
              { color: bot.needs_attention || bot.unread ? p.text : p.textDim },
              bot.needs_attention && styles.bold,
            ]}
          >
            {parent ? `${parent}'s helper · ` : ""}
            {subtitle}
          </Text>
          {bot.needs_attention ? (
            <View style={[styles.badge, { backgroundColor: p.accent }]}>
              <Text style={[styles.badgeText, { color: p.onAccent }]}>!</Text>
            </View>
          ) : bot.unread ? (
            <View style={[styles.dot, { backgroundColor: p.accent }]} />
          ) : null}
        </View>
      </View>
    </Pressable>
  );
}

function EmptyState() {
  const p = usePalette();
  return (
    <View style={styles.emptyWrap}>
      <View style={styles.crew}>
        <BotAvatar look={PRESETS.Sprout} size={52} />
        <BotAvatar look={PRESETS.Telly} size={76} />
        <BotAvatar look={PRESETS.Beacon} size={52} />
      </View>
      <Text style={[styles.emptyTitle, { color: p.text }]}>Hire your first bot</Text>
      <Text style={[styles.emptyLead, { color: p.textDim }]}>
        Each bot has its own browser on your server. Give it a job and it works on it, asking you
        before anything that matters.
      </Text>
      <Button
        title="Create a bot"
        icon="add"
        onPress={() => router.push("/new")}
        style={{ alignSelf: "stretch" }}
      />
    </View>
  );
}

const styles = StyleSheet.create({
  header: {
    flexDirection: "row",
    alignItems: "center",
    paddingHorizontal: 14,
    paddingVertical: 6,
    gap: 8,
  },
  me: {
    width: 36,
    height: 36,
    borderRadius: 18,
    borderWidth: StyleSheet.hairlineWidth,
    alignItems: "center",
    justifyContent: "center",
  },
  meText: { fontSize: font.body, fontWeight: "700" },
  brand: { flex: 1, textAlign: "center", fontSize: 20, fontWeight: "800", letterSpacing: -0.4 },
  search: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    marginHorizontal: 16,
    marginTop: 6,
    marginBottom: 8,
    paddingHorizontal: 14,
    borderRadius: radius.pill,
    height: 42,
  },
  searchInput: { flex: 1, fontSize: font.body, paddingVertical: 0 },
  pad: { paddingHorizontal: 16, paddingVertical: 8, gap: 10 },
  row: {
    flexDirection: "row",
    alignItems: "center",
    gap: 14,
    paddingHorizontal: 16,
    paddingVertical: 10,
  },
  rowBody: { flex: 1, gap: 3 },
  rowTop: { flexDirection: "row", alignItems: "center", gap: 6 },
  name: { fontSize: font.body, fontWeight: "600", flexShrink: 1 },
  bold: { fontWeight: "700" },
  time: { fontSize: font.small },
  preview: { flex: 1, fontSize: font.small + 1 },
  dot: { width: 9, height: 9, borderRadius: 5 },
  badge: {
    minWidth: 20,
    height: 20,
    borderRadius: 10,
    alignItems: "center",
    justifyContent: "center",
  },
  badgeText: { fontSize: font.small, fontWeight: "800" },
  empty: { textAlign: "center", marginTop: 48, fontSize: font.body },
  emptyWrap: { flex: 1, alignItems: "center", justifyContent: "center", padding: 32, gap: 14 },
  crew: { flexDirection: "row", alignItems: "flex-end", gap: 8, marginBottom: 8 },
  emptyTitle: { fontSize: 24, fontWeight: "700" },
  emptyLead: { fontSize: font.body, lineHeight: 23, textAlign: "center", marginBottom: 12 },
  fabWrap: { position: "absolute", left: 0, right: 0, alignItems: "center" },
  fab: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    paddingHorizontal: 22,
    height: 50,
    borderRadius: radius.pill,
    shadowColor: "#000",
    shadowOpacity: 0.3,
    shadowRadius: 12,
    shadowOffset: { width: 0, height: 4 },
    elevation: 6,
  },
  fabText: { fontSize: font.body, fontWeight: "700" },
});
