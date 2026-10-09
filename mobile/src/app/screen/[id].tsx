import { Ionicons } from "@expo/vector-icons";
import { router, useLocalSearchParams } from "expo-router";
import { useEffect, useRef, useState } from "react";
import { KeyboardAvoidingView, Pressable, StyleSheet, Text, TextInput, View } from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { WebView, type WebViewMessageEvent } from "react-native-webview";
import { Button, IconButton, type IconName, Notice, tap } from "@/components/ui";
import {
  type HumanInput,
  VIEWPORT,
  getServer,
  sendInputs,
  setController,
  streamUrl,
} from "@/lib/api";
import { font, radius, usePalette } from "@/lib/theme";

/**
 * The bot's own screen, live. Watching is free; "Take control" pauses the bot and
 * hands you its browser — tap to click, type into the field below, or open an address —
 * for the moments only a person can handle: a sign-in, a CAPTCHA, a confirmation.
 * Leaving the screen gives control back.
 *
 * The picture is the server's MJPEG stream shown in a web view whose base URL is the
 * server, so it carries the session cookie; taps come back from the page as fractions
 * of the picture and become clicks in the bot's 1280×800 viewport.
 */
export default function LiveScreen() {
  const { id, name } = useLocalSearchParams<{ id: string; name?: string }>();
  const p = usePalette();
  const [human, setHuman] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [text, setText] = useState("");
  const [address, setAddress] = useState("");
  const [lastUrl, setLastUrl] = useState("");
  const holding = useRef(false);

  // Give the screen back however this view closes.
  useEffect(
    () => () => {
      if (holding.current) void setController(id, "bot").catch(() => undefined);
    },
    [id],
  );

  const send = async (events: HumanInput[]) => {
    try {
      const result = await sendInputs(id, events);
      if (result.url) setLastUrl(result.url);
      setError(null);
    } catch (cause) {
      setError((cause as Error).message);
    }
  };

  const toggle = async () => {
    setBusy(true);
    setError(null);
    try {
      const next = human ? "bot" : "human";
      await setController(id, next);
      holding.current = next === "human";
      setHuman(next === "human");
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const onMessage = (event: WebViewMessageEvent) => {
    if (!human) return;
    let data: { fx: number; fy: number };
    try {
      data = JSON.parse(event.nativeEvent.data);
    } catch {
      return;
    }
    tap();
    const x = Math.round(Math.min(Math.max(data.fx, 0), 1) * (VIEWPORT.width - 1));
    const y = Math.round(Math.min(Math.max(data.fy, 0), 1) * (VIEWPORT.height - 1));
    void send([
      { kind: "move", x, y },
      { kind: "down", x, y, button: "left", clicks: 1 },
      { kind: "up", x, y, button: "left", clicks: 1 },
    ]);
  };

  const scroll = (dy: number) =>
    void send([{ kind: "wheel", x: VIEWPORT.width / 2, y: VIEWPORT.height / 2, dx: 0, dy }]);

  const html = `<!doctype html><html><head>
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=4">
<style>
  html,body{margin:0;height:100%;background:${p.bg};display:flex;align-items:center;justify-content:center}
  img{width:100%;height:auto;display:block;-webkit-user-select:none;user-select:none;-webkit-touch-callout:none}
  #off{color:${p.textFaint};font:14px -apple-system,system-ui,sans-serif;text-align:center;padding:24px;display:none}
</style></head><body>
<img id="s" src="${streamUrl(id)}" alt="">
<div id="off">The screen isn't available right now.</div>
<script>
  var s=document.getElementById('s');
  s.onerror=function(){s.style.display='none';document.getElementById('off').style.display='block'};
  s.addEventListener('click',function(e){
    var r=s.getBoundingClientRect();
    window.ReactNativeWebView.postMessage(JSON.stringify({fx:(e.clientX-r.left)/r.width,fy:(e.clientY-r.top)/r.height}));
  });
</script></body></html>`;

  return (
    <SafeAreaView style={{ flex: 1, backgroundColor: p.bg }}>
      <View style={styles.header}>
        <IconButton name="close" label="Close" onPress={() => router.back()} />
        <View style={{ flex: 1, alignItems: "center" }}>
          <Text style={[styles.title, { color: p.text }]} numberOfLines={1}>
            {name ? `${name}'s screen` : "Screen"}
          </Text>
          <View style={styles.liveRow}>
            <View style={[styles.liveDot, { backgroundColor: human ? "#ffb02e" : "#3dffb0" }]} />
            <Text style={[styles.sub, { color: p.textFaint }]}>
              {human ? "You have control — the bot is paused" : "Live"}
            </Text>
          </View>
        </View>
        <View style={{ width: 40 }} />
      </View>

      <KeyboardAvoidingView behavior="padding" style={{ flex: 1 }}>
        <View style={[styles.stage, { borderColor: human ? "#ffb02e" : p.line }]}>
          <WebView
            source={{ html, baseUrl: getServer() }}
            originWhitelist={["*"]}
            sharedCookiesEnabled
            thirdPartyCookiesEnabled
            onMessage={onMessage}
            scrollEnabled={false}
            style={{ backgroundColor: p.bg }}
            containerStyle={{ backgroundColor: p.bg }}
          />
        </View>
        {lastUrl ? (
          <Text numberOfLines={1} style={[styles.url, { color: p.textFaint }]}>
            {lastUrl}
          </Text>
        ) : null}

        <View style={styles.controls}>
          {error && <Notice>{error}</Notice>}
          {human ? (
            <>
              <Text style={[styles.help, { color: p.textDim }]}>
                Tap the picture to click. Pinch to zoom.
              </Text>
              <View style={styles.row}>
                <Tool
                  icon="arrow-back"
                  label="Back"
                  onPress={() => void send([{ kind: "back" }])}
                />
                <Tool
                  icon="refresh"
                  label="Reload"
                  onPress={() => void send([{ kind: "reload" }])}
                />
                <Tool icon="arrow-up" label="Scroll up" onPress={() => scroll(-500)} />
                <Tool icon="arrow-down" label="Scroll down" onPress={() => scroll(500)} />
                <Tool
                  icon="return-down-back"
                  label="Enter"
                  onPress={() => void send([{ kind: "key", key: "Enter" }])}
                />
              </View>
              <Field
                value={text}
                onChange={setText}
                placeholder="Type into the focused field"
                icon="arrow-up"
                onSubmit={() => {
                  if (!text) return;
                  void send([{ kind: "type", text }]);
                  setText("");
                }}
              />
              <Field
                value={address}
                onChange={setAddress}
                placeholder="Go to address"
                icon="globe-outline"
                onSubmit={() => {
                  const raw = address.trim();
                  if (!raw) return;
                  void send([
                    { kind: "navigate", url: /^https?:\/\//.test(raw) ? raw : `https://${raw}` },
                  ]);
                  setAddress("");
                }}
              />
              <Button title="Give control back" onPress={() => void toggle()} busy={busy} />
            </>
          ) : (
            <>
              <Text style={[styles.help, { color: p.textDim }]}>
                Take control to sign in, solve a CAPTCHA or finish a step yourself. The bot pauses
                until you hand it back.
              </Text>
              <Button
                title="Take control"
                icon="hand-left-outline"
                onPress={() => void toggle()}
                busy={busy}
              />
            </>
          )}
        </View>
      </KeyboardAvoidingView>
    </SafeAreaView>
  );
}

function Tool({ icon, label, onPress }: { icon: IconName; label: string; onPress: () => void }) {
  const p = usePalette();
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={label}
      onPress={() => {
        tap();
        onPress();
      }}
      style={({ pressed }) => [styles.tool, { backgroundColor: pressed ? p.pressed : p.raised }]}
    >
      <Ionicons name={icon} size={18} color={p.text} />
    </Pressable>
  );
}

function Field({
  value,
  onChange,
  placeholder,
  icon,
  onSubmit,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder: string;
  icon: IconName;
  onSubmit: () => void;
}) {
  const p = usePalette();
  return (
    <View style={[styles.field, { backgroundColor: p.raised }]}>
      <TextInput
        value={value}
        onChangeText={onChange}
        placeholder={placeholder}
        placeholderTextColor={p.textFaint}
        autoCapitalize="none"
        autoCorrect={false}
        returnKeyType="send"
        onSubmitEditing={onSubmit}
        keyboardAppearance={p.scheme}
        style={[styles.fieldInput, { color: p.text }]}
      />
      <Pressable
        accessibilityLabel={placeholder}
        onPress={onSubmit}
        style={[styles.fieldGo, { backgroundColor: p.accent }]}
      >
        <Ionicons name={icon} size={16} color={p.onAccent} />
      </Pressable>
    </View>
  );
}

const styles = StyleSheet.create({
  header: { flexDirection: "row", alignItems: "center", paddingHorizontal: 6, paddingVertical: 4 },
  title: { fontSize: font.heading, fontWeight: "700" },
  liveRow: { flexDirection: "row", alignItems: "center", gap: 6 },
  liveDot: { width: 7, height: 7, borderRadius: 4 },
  sub: { fontSize: font.tiny + 1 },
  stage: {
    marginHorizontal: 12,
    marginTop: 8,
    aspectRatio: VIEWPORT.width / VIEWPORT.height,
    borderRadius: radius.md,
    borderWidth: 1.5,
    overflow: "hidden",
  },
  url: { fontSize: font.tiny, marginHorizontal: 16, marginTop: 6 },
  controls: { padding: 16, gap: 10, marginTop: "auto" },
  help: { fontSize: font.small, lineHeight: 19, textAlign: "center" },
  row: { flexDirection: "row", justifyContent: "space-between", gap: 8 },
  tool: {
    flex: 1,
    height: 44,
    borderRadius: radius.md,
    alignItems: "center",
    justifyContent: "center",
  },
  field: {
    flexDirection: "row",
    alignItems: "center",
    borderRadius: radius.pill,
    paddingLeft: 18,
    paddingRight: 5,
    height: 48,
  },
  fieldInput: { flex: 1, fontSize: font.body, paddingVertical: 0 },
  fieldGo: {
    width: 38,
    height: 38,
    borderRadius: 19,
    alignItems: "center",
    justifyContent: "center",
  },
});
