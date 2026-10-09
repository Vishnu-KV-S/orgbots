import { router } from "expo-router";
import { useState } from "react";
import {
  KeyboardAvoidingView,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { BotAvatar } from "@/components/BotAvatar";
import { Button, IconButton, Notice, tap } from "@/components/ui";
import { createBot } from "@/lib/api";
import { TEMPLATES } from "@/lib/templates";
import { font, radius, usePalette } from "@/lib/theme";

/**
 * Hire a bot: pick a starting point, name it, say what it is for. Everything else —
 * duties, boundaries, its look — can be refined later in the web app.
 */
export default function NewBot() {
  const p = usePalette();
  const insets = useSafeAreaInsets();
  const [chosen, setChosen] = useState(0);
  const template = TEMPLATES[chosen];
  const [name, setName] = useState(template.name);
  const [mission, setMission] = useState(template.brief.mission);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const choose = (i: number) => {
    tap();
    setChosen(i);
    setName(TEMPLATES[i].name);
    setMission(TEMPLATES[i].brief.mission);
  };

  const create = async () => {
    setBusy(true);
    setError(null);
    try {
      const { blurb: _blurb, ...draft } = template;
      const bot = await createBot({
        ...draft,
        name: name.trim(),
        brief: { ...draft.brief, mission: mission.trim() },
      });
      router.dismiss();
      router.push({ pathname: "/chat/[id]", params: { id: bot.id } });
    } catch (cause) {
      setError((cause as Error).message);
      setBusy(false);
    }
  };

  return (
    <View style={{ flex: 1, backgroundColor: p.bg }}>
      <View style={styles.header}>
        <Text style={[styles.title, { color: p.text }]}>New bot</Text>
        <IconButton name="close" label="Close" onPress={() => router.back()} filled />
      </View>
      <KeyboardAvoidingView behavior="padding" style={{ flex: 1 }}>
        <ScrollView
          contentContainerStyle={[styles.page, { paddingBottom: insets.bottom + 24 }]}
          keyboardShouldPersistTaps="handled"
        >
          <Text style={[styles.section, { color: p.textFaint }]}>Start from</Text>
          <View style={styles.grid}>
            {TEMPLATES.map((t, i) => {
              const on = i === chosen;
              return (
                <Pressable
                  key={t.name}
                  accessibilityRole="radio"
                  accessibilityState={{ selected: on }}
                  onPress={() => choose(i)}
                  style={[
                    styles.card,
                    {
                      backgroundColor: on ? p.raised : p.surface,
                      borderColor: on ? p.accent : p.line,
                    },
                  ]}
                >
                  <BotAvatar look={t.appearance} size={44} working={on} />
                  <Text style={[styles.cardName, { color: p.text }]}>{t.name}</Text>
                  <Text numberOfLines={2} style={[styles.cardBlurb, { color: p.textDim }]}>
                    {t.blurb}
                  </Text>
                </Pressable>
              );
            })}
          </View>

          <Text style={[styles.section, { color: p.textFaint }]}>Name</Text>
          <TextInput
            value={name}
            onChangeText={setName}
            maxLength={60}
            keyboardAppearance={p.scheme}
            style={[
              styles.input,
              { color: p.text, backgroundColor: p.raised, borderColor: p.line },
            ]}
          />
          <Text style={[styles.section, { color: p.textFaint }]}>What is it for?</Text>
          <TextInput
            value={mission}
            onChangeText={setMission}
            multiline
            placeholder="e.g. Keep an eye on competitor pricing and tell me when it changes."
            placeholderTextColor={p.textFaint}
            keyboardAppearance={p.scheme}
            style={[
              styles.input,
              styles.area,
              { color: p.text, backgroundColor: p.raised, borderColor: p.line },
            ]}
          />
          {error && <Notice>{error}</Notice>}
          <Button
            title={`Create ${name.trim() || "bot"}`}
            onPress={() => void create()}
            busy={busy}
            disabled={!name.trim()}
          />
        </ScrollView>
      </KeyboardAvoidingView>
    </View>
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
  page: { padding: 20, gap: 12 },
  section: {
    fontSize: font.small,
    fontWeight: "600",
    textTransform: "uppercase",
    letterSpacing: 0.6,
    marginTop: 8,
  },
  grid: { flexDirection: "row", flexWrap: "wrap", gap: 10 },
  card: {
    width: "48%",
    flexGrow: 1,
    borderRadius: radius.lg,
    borderWidth: 1,
    padding: 14,
    gap: 6,
  },
  cardName: { fontSize: font.body, fontWeight: "700", marginTop: 4 },
  cardBlurb: { fontSize: font.small, lineHeight: 18 },
  input: {
    fontSize: font.body,
    borderRadius: radius.md,
    borderWidth: StyleSheet.hairlineWidth,
    paddingHorizontal: 16,
    paddingVertical: 14,
  },
  area: { minHeight: 96, textAlignVertical: "top" },
});
