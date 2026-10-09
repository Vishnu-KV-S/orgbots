import { Ionicons } from "@expo/vector-icons";
import Constants from "expo-constants";
import * as Linking from "expo-linking";
import { router } from "expo-router";
import { Alert, Pressable, ScrollView, StyleSheet, Text, View } from "react-native";
import { IconButton, type IconName } from "@/components/ui";
import { getServer } from "@/lib/api";
import { useSession } from "@/lib/session";
import { font, radius, usePalette } from "@/lib/theme";

export default function Settings() {
  const p = usePalette();
  const { server, mode, me, signOut, disconnect } = useSession();

  const confirmDisconnect = () =>
    Alert.alert("Change server?", "You'll be signed out of this one.", [
      { text: "Cancel", style: "cancel" },
      {
        text: "Change server",
        style: "destructive",
        onPress: () => {
          router.dismissAll();
          void disconnect();
        },
      },
    ]);

  return (
    <View style={{ flex: 1, backgroundColor: p.bg }}>
      <View style={styles.header}>
        <Text style={[styles.title, { color: p.text }]}>Settings</Text>
        <IconButton name="close" label="Close" onPress={() => router.back()} filled />
      </View>
      <ScrollView contentContainerStyle={styles.page}>
        {me && (
          <View style={[styles.group, { backgroundColor: p.surface, borderColor: p.line }]}>
            <View style={styles.profile}>
              <View style={[styles.avatar, { backgroundColor: p.raised }]}>
                <Text style={[styles.avatarText, { color: p.text }]}>
                  {(me.name || me.email).slice(0, 1).toUpperCase()}
                </Text>
              </View>
              <View style={{ flex: 1 }}>
                <Text style={[styles.name, { color: p.text }]}>{me.name || me.email}</Text>
                <Text style={[styles.sub, { color: p.textDim }]}>
                  {me.email} · {me.role}
                </Text>
              </View>
            </View>
          </View>
        )}

        <Text style={[styles.section, { color: p.textFaint }]}>Server</Text>
        <View style={[styles.group, { backgroundColor: p.surface, borderColor: p.line }]}>
          <Row
            icon="server-outline"
            label={server.replace(/^https?:\/\//, "")}
            detail={mode === "members" ? "Team sign-in" : "Single user"}
          />
          <Row
            icon="open-outline"
            label="Open the web app"
            detail="Briefs, files, skills, routines and apps"
            onPress={() => void Linking.openURL(getServer())}
          />
          <Row icon="swap-horizontal-outline" label="Change server" onPress={confirmDisconnect} />
          {mode === "members" && (
            <Row
              icon="log-out-outline"
              label="Sign out"
              danger
              onPress={() => {
                router.dismissAll();
                void signOut();
              }}
            />
          )}
        </View>

        <Text style={[styles.foot, { color: p.textFaint }]}>
          Orgbots {Constants.expoConfig?.version ?? ""} · open source, MIT
        </Text>
      </ScrollView>
    </View>
  );
}

function Row({
  icon,
  label,
  detail,
  onPress,
  danger,
}: {
  icon: IconName;
  label: string;
  detail?: string;
  onPress?: () => void;
  danger?: boolean;
}) {
  const p = usePalette();
  const color = danger ? "#ff6b5e" : p.text;
  return (
    <Pressable
      disabled={!onPress}
      onPress={onPress}
      style={({ pressed }) => [styles.row, pressed && { backgroundColor: p.raised }]}
    >
      <Ionicons name={icon} size={20} color={color} />
      <View style={{ flex: 1 }}>
        <Text style={[styles.rowLabel, { color }]} numberOfLines={1}>
          {label}
        </Text>
        {detail && <Text style={[styles.sub, { color: p.textFaint }]}>{detail}</Text>}
      </View>
      {onPress && !danger && <Ionicons name="chevron-forward" size={16} color={p.textFaint} />}
    </Pressable>
  );
}

const styles = StyleSheet.create({
  header: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    paddingHorizontal: 20,
    paddingTop: 18,
    paddingBottom: 6,
  },
  title: { fontSize: font.title, fontWeight: "800", letterSpacing: -0.5 },
  page: { padding: 20, gap: 10 },
  section: {
    fontSize: font.small,
    fontWeight: "600",
    textTransform: "uppercase",
    letterSpacing: 0.6,
    marginTop: 12,
    marginLeft: 4,
  },
  group: { borderRadius: radius.lg, borderWidth: StyleSheet.hairlineWidth, overflow: "hidden" },
  profile: { flexDirection: "row", alignItems: "center", gap: 14, padding: 16 },
  avatar: {
    width: 48,
    height: 48,
    borderRadius: 24,
    alignItems: "center",
    justifyContent: "center",
  },
  avatarText: { fontSize: 20, fontWeight: "700" },
  name: { fontSize: font.heading, fontWeight: "700" },
  sub: { fontSize: font.small, marginTop: 2 },
  row: {
    flexDirection: "row",
    alignItems: "center",
    gap: 14,
    paddingHorizontal: 16,
    paddingVertical: 14,
  },
  rowLabel: { fontSize: font.body },
  foot: { textAlign: "center", fontSize: font.small, marginTop: 24 },
});
