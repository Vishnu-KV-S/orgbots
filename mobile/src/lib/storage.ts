import * as SecureStore from "expo-secure-store";
import { Platform } from "react-native";

/**
 * Small key-value settings: the OS keychain on a phone, `localStorage` on the web
 * build (where SecureStore has no implementation).
 */
const web = Platform.OS === "web";

export async function getItem(key: string): Promise<string | null> {
  if (web) return globalThis.localStorage?.getItem(key) ?? null;
  return SecureStore.getItemAsync(key);
}

export async function setItem(key: string, value: string): Promise<void> {
  if (web) return void globalThis.localStorage?.setItem(key, value);
  await SecureStore.setItemAsync(key, value);
}

export async function removeItem(key: string): Promise<void> {
  if (web) return void globalThis.localStorage?.removeItem(key);
  await SecureStore.deleteItemAsync(key);
}
