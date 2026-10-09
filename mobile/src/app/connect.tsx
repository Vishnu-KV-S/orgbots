import { LinearGradient } from "expo-linear-gradient";
import { useState } from "react";
import {
  KeyboardAvoidingView,
  Platform,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { BotAvatar } from "@/components/BotAvatar";
import { Button, Notice } from "@/components/ui";
import { PRESETS } from "@/lib/appearance";
import { useSession } from "@/lib/session";
import { font, radius, usePalette } from "@/lib/theme";

/**
 * First run: which Orgbots server is this phone's. The address is the one people open
 * in a browser — the app reaches the API through that same web server.
 */
export default function Connect() {
  const p = usePalette();
  const { connect, server } = useSession();
  const [address, setAddress] = useState(server.replace(/^https:\/\//, ""));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const go = async () => {
    setBusy(true);
    setError(null);
    try {
      await connect(address);
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <View style={{ flex: 1, backgroundColor: p.bg }}>
      <LinearGradient
        colors={p.scheme === "dark" ? ["#1a1408", "#000000"] : ["#fff4e0", "#ffffff"]}
        style={StyleSheet.absoluteFill}
        end={{ x: 0.5, y: 0.6 }}
      />
      <SafeAreaView style={{ flex: 1 }}>
        <KeyboardAvoidingView
          behavior={Platform.OS === "ios" ? "padding" : undefined}
          style={{ flex: 1 }}
        >
          <ScrollView contentContainerStyle={styles.page} keyboardShouldPersistTaps="handled">
            <View style={styles.hero}>
              <View style={styles.crew}>
                <BotAvatar look={PRESETS.Cubey} size={56} />
                <BotAvatar look={PRESETS.Orbit} size={84} />
                <BotAvatar look={PRESETS.Beacon} size={56} />
              </View>
              <Text style={[styles.title, { color: p.text }]}>Orgbots</Text>
              <Text style={[styles.lead, { color: p.textDim }]}>
                Your AI employees, in your pocket. Connect to the Orgbots server your team runs.
              </Text>
            </View>
            <View style={styles.form}>
              <Text style={[styles.label, { color: p.textDim }]}>Server address</Text>
              <TextInput
                value={address}
                onChangeText={setAddress}
                placeholder="bots.example.com"
                placeholderTextColor={p.textFaint}
                autoCapitalize="none"
                autoCorrect={false}
                keyboardType="url"
                textContentType="URL"
                returnKeyType="go"
                onSubmitEditing={() => void go()}
                keyboardAppearance={p.scheme}
                style={[
                  styles.input,
                  { color: p.text, backgroundColor: p.raised, borderColor: p.line },
                ]}
              />
              {error && <Notice>{error}</Notice>}
              <Button
                title="Connect"
                onPress={() => void go()}
                busy={busy}
                disabled={!address.trim()}
              />
              <Text style={[styles.hint, { color: p.textFaint }]}>
                The same address you open in a browser. HTTPS is assumed; type http:// for a server
                on your own network.
              </Text>
            </View>
          </ScrollView>
        </KeyboardAvoidingView>
      </SafeAreaView>
    </View>
  );
}

const styles = StyleSheet.create({
  page: { flexGrow: 1, justifyContent: "space-between", padding: 24, gap: 32 },
  hero: { alignItems: "center", marginTop: 48, gap: 14 },
  crew: { flexDirection: "row", alignItems: "flex-end", gap: 10, marginBottom: 12 },
  title: { fontSize: 40, fontWeight: "800", letterSpacing: -1 },
  lead: { fontSize: font.body, lineHeight: 23, textAlign: "center", maxWidth: 320 },
  form: { gap: 12 },
  label: { fontSize: font.small, marginLeft: 4 },
  input: {
    fontSize: font.body,
    borderRadius: radius.pill,
    borderWidth: StyleSheet.hairlineWidth,
    paddingHorizontal: 20,
    paddingVertical: 15,
  },
  hint: { fontSize: font.small, lineHeight: 18, textAlign: "center", paddingHorizontal: 12 },
});
