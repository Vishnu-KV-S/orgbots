import { DarkTheme, DefaultTheme, Stack, ThemeProvider, router, useSegments } from "expo-router";
import { StatusBar } from "expo-status-bar";
import * as SystemUI from "expo-system-ui";
import { useEffect } from "react";
import { ActivityIndicator, View } from "react-native";
import { SafeAreaProvider } from "react-native-safe-area-context";
import { SessionProvider, useSession } from "@/lib/session";
import { usePalette } from "@/lib/theme";

export default function RootLayout() {
  return (
    <SafeAreaProvider>
      <SessionProvider>
        <Themed />
      </SessionProvider>
    </SafeAreaProvider>
  );
}

function Themed() {
  const p = usePalette();
  const { status } = useSession();
  useEffect(() => {
    void SystemUI.setBackgroundColorAsync(p.bg).catch(() => undefined);
  }, [p.bg]);
  const base = p.scheme === "dark" ? DarkTheme : DefaultTheme;
  return (
    <ThemeProvider
      value={{
        ...base,
        colors: {
          ...base.colors,
          background: p.bg,
          card: p.bg,
          text: p.text,
          border: p.line,
          primary: p.accent,
        },
      }}
    >
      <StatusBar style={p.scheme === "dark" ? "light" : "dark"} />
      {/* Screens fetch as they mount, so none mounts before the saved server is known. */}
      {status === "loading" ? (
        <Splash />
      ) : (
        <Stack screenOptions={{ headerShown: false, contentStyle: { backgroundColor: p.bg } }}>
          <Stack.Screen name="index" />
          <Stack.Screen name="connect" options={{ animation: "fade" }} />
          <Stack.Screen name="signin" options={{ animation: "fade" }} />
          <Stack.Screen name="chat/[id]" />
          <Stack.Screen name="new" options={{ presentation: "modal" }} />
          <Stack.Screen name="settings" options={{ presentation: "modal" }} />
          <Stack.Screen name="screen/[id]" options={{ presentation: "fullScreenModal" }} />
        </Stack>
      )}
      {status !== "loading" && <Gate />}
    </ThemeProvider>
  );
}

/** Sends the person to whichever first step they are missing: a server, then a session. */
function Gate() {
  const { status } = useSession();
  const segments = useSegments();
  const at = segments[0] ?? "";
  useEffect(() => {
    if (status === "loading") return;
    if (status === "no-server" && at !== "connect") router.replace("/connect");
    else if (status === "signed-out" && at !== "signin") router.replace("/signin");
    else if (
      (status === "ready" || status === "offline") &&
      (at === "connect" || at === "signin")
    ) {
      router.replace("/");
    }
  }, [status, at]);
  return null;
}

function Splash() {
  const p = usePalette();
  return (
    <View
      style={{ flex: 1, backgroundColor: p.bg, alignItems: "center", justifyContent: "center" }}
    >
      <ActivityIndicator color={p.textDim} />
    </View>
  );
}
