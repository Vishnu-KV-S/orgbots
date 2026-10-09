import { Ionicons } from "@expo/vector-icons";
import { useRef, useState } from "react";
import { StyleSheet, Switch, Text, TextInput, type TextInputProps, View } from "react-native";
import {
  type BotMessage,
  type CredentialField,
  type CredentialKind,
  cancelCredentials,
  chooseSavedLogin,
  submitCredentials,
} from "@/lib/api";
import { font, radius, usePalette } from "@/lib/theme";
import { Screenshot } from "./Screenshot";
import { Button, Notice } from "./ui";

const TITLE = {
  sign_in: "Sign in to",
  sign_up: "Create an account on",
  verify: "Verification code for",
} as const;
const SUBMIT = { sign_in: "Sign in", sign_up: "Create account", verify: "Verify" } as const;

/** How each kind is typed, so the phone's password manager and one-time-code fill work. */
const INPUT: Record<CredentialKind, TextInputProps> = {
  email: {
    keyboardType: "email-address",
    autoComplete: "username",
    textContentType: "username",
    autoCapitalize: "none",
  },
  username: { autoComplete: "username", textContentType: "username", autoCapitalize: "none" },
  phone: { keyboardType: "phone-pad", autoComplete: "tel", textContentType: "telephoneNumber" },
  password: {
    secureTextEntry: true,
    autoComplete: "current-password",
    textContentType: "password",
    autoCapitalize: "none",
  },
  new_password: {
    secureTextEntry: true,
    autoComplete: "new-password",
    textContentType: "newPassword",
    autoCapitalize: "none",
  },
  confirm_password: {
    secureTextEntry: true,
    autoComplete: "new-password",
    textContentType: "newPassword",
    autoCapitalize: "none",
  },
  otp: {
    keyboardType: "number-pad",
    autoComplete: "one-time-code",
    textContentType: "oneTimeCode",
  },
  name: { autoComplete: "name", textContentType: "name" },
  text: { autoComplete: "off" },
};

const SECRET = new Set<CredentialKind>(["password", "new_password", "confirm_password"]);

/**
 * The bot reached a sign-in, sign-up or code page and needs the person. What is typed
 * here goes in one request to the server's vault and from there into the bot's
 * browser — the bot is told the form was filled, never with what. Values live in a ref
 * until that request, then the inputs are cleared.
 */
export function CredentialCard({
  botId,
  botName,
  message,
  live,
  onDone,
}: {
  botId: string;
  botName: string;
  message: BotMessage;
  live: boolean;
  onDone: () => void;
}) {
  const p = usePalette();
  const pl = message.payload;
  const requestId = pl.credential_request_id ?? "";
  const purpose = pl.purpose ?? "sign_in";
  const fields: CredentialField[] = pl.fields ?? [];
  const saved = Array.isArray(pl.saved) ? pl.saved : [];
  const keepable = fields.some((f) => SECRET.has(f.kind));
  const values = useRef<Record<string, string>>({});
  const inputs = useRef<Record<string, TextInput | null>>({});
  const [save, setSave] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const finish = async (which: string, call: () => Promise<unknown>) => {
    setBusy(which);
    setError(null);
    try {
      await call();
      values.current = {};
      Object.values(inputs.current).forEach((i) => i?.clear());
      onDone();
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const submit = () => {
    const filled: Record<string, string> = {};
    for (const f of fields) filled[f.key] = values.current[f.key] ?? "";
    void finish("submit", () => submitCredentials(botId, requestId, filled, keepable && save));
  };

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
        <Ionicons name="lock-closed" size={16} color={p.text} />
        <Text style={[styles.title, { color: p.text }]} numberOfLines={2}>
          {TITLE[purpose]} {pl.host ?? ""}
        </Text>
      </View>
      {message.content ? (
        <Text style={[styles.reason, { color: p.textDim }]}>
          {botName}: {message.content}
        </Text>
      ) : null}
      {pl.screenshot_id && <Screenshot botId={botId} screenshotId={pl.screenshot_id} />}
      {live && pl.retry && (
        <Notice>{"The last attempt didn’t get past this form. The details may be wrong."}</Notice>
      )}

      {live ? (
        <View style={styles.form}>
          {saved.map((s) => (
            <Button
              key={s.id}
              title={`Use saved login ${s.label}`}
              kind="secondary"
              icon="key-outline"
              busy={busy === s.id}
              onPress={() => void finish(s.id, () => chooseSavedLogin(botId, requestId, s.id))}
            />
          ))}
          {saved.length > 0 && (
            <Text style={[styles.or, { color: p.textFaint }]}>or enter details</Text>
          )}
          {fields.map((f, i) => (
            <View key={f.key} style={styles.field}>
              <Text style={[styles.label, { color: p.textDim }]}>{f.label}</Text>
              <TextInput
                ref={(el) => {
                  inputs.current[f.key] = el;
                }}
                {...INPUT[f.kind]}
                autoCorrect={false}
                keyboardAppearance={p.scheme}
                returnKeyType={i === fields.length - 1 ? "go" : "next"}
                onSubmitEditing={() =>
                  i === fields.length - 1 ? submit() : inputs.current[fields[i + 1].key]?.focus()
                }
                onChangeText={(v) => {
                  values.current[f.key] = v;
                }}
                style={[
                  styles.input,
                  { color: p.text, backgroundColor: p.raised, borderColor: p.line },
                ]}
              />
            </View>
          ))}
          {keepable && (
            <View style={styles.saveRow}>
              <Text style={[styles.saveText, { color: p.textDim }]}>
                Save this login so {botName} can sign in again without asking
              </Text>
              <Switch value={save} onValueChange={setSave} />
            </View>
          )}
          {error && <Notice>{error}</Notice>}
          <Button title={SUBMIT[purpose]} onPress={submit} busy={busy === "submit"} />
          <Button
            title="Not now"
            kind="ghost"
            busy={busy === "cancel"}
            onPress={() => void finish("cancel", () => cancelCredentials(botId, requestId))}
          />
          <Text style={[styles.fine, { color: p.textFaint }]}>
            {`Sent to your Orgbots server’s encrypted vault. ${botName} never sees what you type.`}
          </Text>
        </View>
      ) : (
        <Text style={[styles.or, { color: p.textFaint }]}>Answered</Text>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  card: { borderRadius: radius.lg, borderWidth: 1, padding: 16, gap: 10 },
  titleRow: { flexDirection: "row", alignItems: "center", gap: 8 },
  title: { fontSize: font.body, fontWeight: "600", flex: 1 },
  reason: { fontSize: font.small, lineHeight: 19 },
  form: { gap: 10 },
  or: { fontSize: font.small, textAlign: "center" },
  field: { gap: 6 },
  label: { fontSize: font.small },
  input: {
    fontSize: font.body,
    borderRadius: radius.md,
    borderWidth: StyleSheet.hairlineWidth,
    paddingHorizontal: 14,
    paddingVertical: 12,
  },
  saveRow: { flexDirection: "row", alignItems: "center", gap: 12 },
  saveText: { flex: 1, fontSize: font.small, lineHeight: 18 },
  fine: { fontSize: font.tiny, textAlign: "center", lineHeight: 15 },
});
