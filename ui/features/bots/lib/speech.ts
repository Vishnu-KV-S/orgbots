/**
 * The browser's own speech: recognition for dictation and voice chat, synthesis for
 * voice memos and spoken replies. Nothing here leaves the browser through the runtime —
 * there is no speech provider on the server — and everything degrades to "not offered"
 * where the browser has no support.
 */

export interface RecognitionResultList {
  length: number;
  [index: number]: { isFinal?: boolean; length: number; [i: number]: { transcript: string } };
}

export interface SpeechRecognitionLike {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  start: () => void;
  stop: () => void;
  abort?: () => void;
  onresult: ((e: { resultIndex?: number; results: RecognitionResultList }) => void) | null;
  onend: (() => void) | null;
  onerror?: ((e: { error?: string }) => void) | null;
  onspeechstart?: (() => void) | null;
}

export function speechRecognition(): (new () => SpeechRecognitionLike) | null {
  if (typeof window === "undefined") return null;
  const w = window as unknown as Record<string, unknown>;
  return (w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null) as
    (new () => SpeechRecognitionLike) | null;
}

export function canSpeak(): boolean {
  return typeof window !== "undefined" && "speechSynthesis" in window;
}

/** Text as it should be read aloud: no markdown, no bare URLs read letter by letter. */
export function forSpeech(text: string): string {
  return text
    .replace(/```[\s\S]*?```/g, " (code) ")
    .replace(/`([^`]*)`/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
    .replace(/https?:\/\/\S+/g, " (a link) ")
    .replace(/[*_#>|]+/g, " ")
    .replace(/^\s*[-•]\s+/gm, "")
    .replace(/\s+/g, " ")
    .trim();
}

const VOICE_KEY = "bot-voice";

export function savedVoice(): string {
  try {
    return localStorage.getItem(VOICE_KEY) ?? "";
  } catch {
    return "";
  }
}

export function saveVoice(name: string): void {
  try {
    localStorage.setItem(VOICE_KEY, name);
  } catch {
    /* the choice just won't be remembered */
  }
}

/** Speak `text`; resolves when it has been spoken or was stopped. */
export function speak(text: string, voiceName = savedVoice()): Promise<void> {
  return new Promise((resolve) => {
    if (!canSpeak()) return resolve();
    const utterance = new SpeechSynthesisUtterance(forSpeech(text));
    const voice = window.speechSynthesis.getVoices().find((v) => v.name === voiceName);
    if (voice) utterance.voice = voice;
    utterance.onend = () => resolve();
    utterance.onerror = () => resolve();
    window.speechSynthesis.speak(utterance);
  });
}

export function stopSpeaking(): void {
  if (canSpeak()) window.speechSynthesis.cancel();
}
