import * as Linking from "expo-linking";
import { Fragment, type ReactNode } from "react";
import { Platform, StyleSheet, Text, View } from "react-native";
import { INLINE } from "@/lib/text";
import { font, usePalette } from "@/lib/theme";

/**
 * A bot's reply, rendered as text. Replies carry what bots read off web pages, so they
 * are untrusted: the few things a reply uses (links, `code`, **bold**, bullet and
 * numbered lines) become styled text and everything else stays text.
 */
export function RichText({ text, color }: { text: string; color?: string }) {
  const p = usePalette();
  const base = { color: color ?? p.text };

  const inline = (line: string, key: string): ReactNode[] => {
    const out: ReactNode[] = [];
    let last = 0;
    let i = 0;
    for (const match of line.matchAll(INLINE)) {
      const at = match.index ?? 0;
      if (at > last) out.push(line.slice(last, at));
      const token = match[0];
      const k = `${key}-${i++}`;
      if (token.startsWith("`")) {
        out.push(
          <Text key={k} style={[styles.code, { backgroundColor: p.raised, color: p.text }]}>
            {token.slice(1, -1)}
          </Text>,
        );
      } else if (token.startsWith("**")) {
        out.push(
          <Text key={k} style={styles.bold}>
            {token.slice(2, -2)}
          </Text>,
        );
      } else {
        out.push(
          <Text key={k} style={styles.link} onPress={() => void Linking.openURL(token)}>
            {token}
          </Text>,
        );
      }
      last = at + token.length;
    }
    if (last < line.length) out.push(line.slice(last));
    return out;
  };

  const blocks: ReactNode[] = [];
  let paragraph: string[] = [];
  const flush = (key: string) => {
    if (paragraph.length === 0) return;
    const lines = paragraph;
    blocks.push(
      <Text key={key} style={[styles.body, base]} selectable>
        {lines.map((l, i) => (
          <Fragment key={i}>
            {inline(l, `${key}-${i}`)}
            {i < lines.length - 1 ? "\n" : ""}
          </Fragment>
        ))}
      </Text>,
    );
    paragraph = [];
  };

  text.split("\n").forEach((line, n) => {
    const bullet = /^\s*([-*•]|\d+\.)\s+(.*)$/.exec(line);
    const heading = /^#{1,4}\s+(.*)$/.exec(line);
    if (bullet) {
      flush(`p-${n}`);
      const marker = /\d/.test(bullet[1]) ? bullet[1] : "•";
      blocks.push(
        <View key={`b-${n}`} style={styles.bulletRow}>
          <Text style={[styles.body, base, styles.marker]}>{marker}</Text>
          <Text style={[styles.body, base, { flex: 1 }]} selectable>
            {inline(bullet[2], `b-${n}`)}
          </Text>
        </View>,
      );
    } else if (heading) {
      flush(`p-${n}`);
      blocks.push(
        <Text key={`h-${n}`} style={[styles.body, base, styles.heading]} selectable>
          {inline(heading[1], `h-${n}`)}
        </Text>,
      );
    } else if (line.trim() === "") {
      flush(`p-${n}`);
    } else {
      paragraph.push(line);
    }
  });
  flush("p-end");

  return <View style={styles.wrap}>{blocks}</View>;
}

const styles = StyleSheet.create({
  wrap: { gap: 10 },
  body: { fontSize: font.body, lineHeight: 24 },
  bold: { fontWeight: "700" },
  heading: { fontWeight: "700", fontSize: font.heading, marginTop: 4 },
  link: { textDecorationLine: "underline" },
  code: {
    fontFamily: Platform.select({ ios: "Menlo", default: "monospace" }),
    fontSize: font.small,
  },
  bulletRow: { flexDirection: "row", gap: 8, paddingRight: 8 },
  marker: { minWidth: 14 },
});
