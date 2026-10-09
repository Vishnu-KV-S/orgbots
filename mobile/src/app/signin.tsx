import { useLocalSearchParams } from "expo-router";
import { useMemo, useRef, useState } from "react";
import { ActivityIndicator, Alert, StyleSheet, Text, View } from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { WebView, type WebViewNavigation } from "react-native-webview";
import * as Clipboard from "expo-clipboard";
import { IconButton } from "@/components/ui";
import { useSession } from "@/lib/session";
import { font, usePalette } from "@/lib/theme";

/**
 * Signing in, for a server with members. Rather than re-implement sign-in, the app
 * opens the server's own `/signin` page — invitation links and single sign-on both
 * work there already — in a web view that shares its cookies with the app. When the
 * page sends the browser on into the web app, sign-in is done: the session cookie it
 * set is now the app's too, and the app takes over.
 *
 * `?link=` (from an `orgbots://signin?link=…` deep link or a pasted sign-in link)
 * opens that link directly.
 */
export default function SignIn() {
  const p = usePalette();
  const { server, refresh, disconnect } = useSession();
  const params = useLocalSearchParams<{ link?: string }>();
  const [link, setLink] = useState(params.link ?? "");
  const [loading, setLoading] = useState(true);
  const done = useRef(false);
  const host = useMemo(() => {
    try {
      return new URL(server).host;
    } catch {
      return server;
    }
  }, [server]);

  const start = `${server}/signin?return_to=%2F${link ? `&link=${encodeURIComponent(link)}` : ""}`;

  /** Navigation inside the server's sign-in page and the API's SSO callback is sign-in;
   * anything else on the server means the page has let us in. Identity providers are
   * other hosts and load normally. */
  const intercept = (nav: WebViewNavigation): boolean => {
    let url: URL;
    try {
      url = new URL(nav.url);
    } catch {
      return true;
    }
    if (`${url.protocol}//${url.host}` !== server) return true;
    if (url.pathname.startsWith("/signin") || url.pathname.startsWith("/rt/")) return true;
    if (!done.current) {
      done.current = true;
      void finish();
    }
    return false;
  };

  /** The web view hands its cookies to the app's networking asynchronously on iOS,
   * so the first check can beat the cookie there: try a few times before giving up. */
  const finish = async () => {
    for (let attempt = 0; attempt < 5; attempt++) {
      if ((await refresh()) !== "signed-out") return;
      await new Promise((r) => setTimeout(r, 500));
    }
    done.current = false;
    Alert.alert("Not signed in", "The server didn't recognise this phone's session. Try again.");
  };

  const pasteLink = async () => {
    const text = (await Clipboard.getStringAsync()).trim();
    let token = "";
    try {
      token = new URL(text).searchParams.get("link") ?? "";
    } catch {
      token = /^[A-Za-z0-9_\-.]{16,}$/.test(text) ? text : "";
    }
    if (!token) {
      Alert.alert(
        "No sign-in link",
        "Copy the sign-in link an admin sent you, then tap paste again.",
      );
      return;
    }
    setLoading(true);
    setLink(token);
  };

  return (
    <SafeAreaView style={{ flex: 1, backgroundColor: p.bg }} edges={["top", "bottom"]}>
      <View style={[styles.bar, { borderColor: p.line }]}>
        <IconButton name="arrow-back" label="Change server" onPress={() => void disconnect()} />
        <View style={{ flex: 1, alignItems: "center" }}>
          <Text style={[styles.title, { color: p.text }]}>Sign in</Text>
          <Text style={[styles.host, { color: p.textFaint }]} numberOfLines={1}>
            {host}
          </Text>
        </View>
        <IconButton
          name="clipboard-outline"
          label="Paste a sign-in link"
          onPress={() => void pasteLink()}
        />
      </View>
      <View style={{ flex: 1 }}>
        <WebView
          key={start}
          source={{ uri: start }}
          sharedCookiesEnabled
          thirdPartyCookiesEnabled
          onShouldStartLoadWithRequest={intercept}
          onNavigationStateChange={(nav) => {
            if (!nav.loading) intercept(nav);
          }}
          onLoadEnd={() => setLoading(false)}
          style={{ backgroundColor: p.bg }}
          containerStyle={{ backgroundColor: p.bg }}
          forceDarkOn={p.scheme === "dark"}
        />
        {loading && (
          <View style={[StyleSheet.absoluteFill, styles.center, { backgroundColor: p.bg }]}>
            <ActivityIndicator color={p.textDim} />
          </View>
        )}
      </View>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  bar: {
    flexDirection: "row",
    alignItems: "center",
    paddingHorizontal: 8,
    paddingVertical: 6,
    borderBottomWidth: StyleSheet.hairlineWidth,
  },
  title: { fontSize: font.heading, fontWeight: "600" },
  host: { fontSize: font.tiny },
  center: { alignItems: "center", justifyContent: "center" },
});
