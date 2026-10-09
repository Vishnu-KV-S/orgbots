"use client";

import { useEffect, useRef, useState } from "react";
import { type Bot, type BotMessage, recordVoiceCall } from "@/lib/api/bots";
import { cx } from "@/lib/cx";
import {
  type SpeechRecognitionLike,
  canSpeak,
  saveVoice,
  savedVoice,
  speak,
  speechRecognition,
  stopSpeaking,
} from "../lib/speech";
import { Avatar } from "./Avatar";

type Phase = "listening" | "waiting" | "speaking" | "paused";

/**
 * A voice chat with one bot. What the person says becomes a message (marked as spoken,
 * so the bot answers in a few spoken sentences), each new reply is read aloud, and the
 * person talking over the bot stops it. Hanging up leaves a card in the conversation;
 * every turn of the call is already there as a message.
 *
 * Speech is the browser's own, recognition and synthesis both.
 */
export function VoiceCall({
  bot,
  messages,
  working,
  onSend,
  onEnd,
}: {
  bot: Bot;
  messages: BotMessage[];
  working: boolean;
  onSend: (text: string) => Promise<boolean>;
  onEnd: () => void;
}) {
  const [phase, setPhase] = useState<Phase>("listening");
  const [heard, setHeard] = useState("");
  const [turns, setTurns] = useState(0);
  const [voices, setVoices] = useState<SpeechSynthesisVoice[]>([]);
  const [voice, setVoice] = useState(savedVoice());
  const [error, setError] = useState<string | null>(null);
  const started = useRef(Date.now());
  const recognition = useRef<SpeechRecognitionLike | null>(null);
  const spokenUpTo = useRef(messages.length ? messages[messages.length - 1].seq : 0);
  const phaseRef = useRef(phase);
  phaseRef.current = phase;
  // The latest `onSend`, read when speech arrives: the recognition session below is
  // started once, and restarting it on every render would drop what was being said.
  const sendRef = useRef(onSend);
  sendRef.current = onSend;

  // The voices load asynchronously in most browsers.
  useEffect(() => {
    if (!canSpeak()) return;
    const load = () => setVoices(window.speechSynthesis.getVoices());
    load();
    window.speechSynthesis.addEventListener("voiceschanged", load);
    return () => window.speechSynthesis.removeEventListener("voiceschanged", load);
  }, []);

  // Listening: one long recognition session, restarted when the browser ends it.
  useEffect(() => {
    const Recognition = speechRecognition();
    if (!Recognition) {
      setError("This browser cannot listen. Try Chrome or Edge.");
      return;
    }
    const r = new Recognition();
    r.continuous = true;
    r.interimResults = true;
    r.lang = navigator.language || "en-US";
    r.onspeechstart = () => {
      // Barge-in: the person talking stops the bot talking.
      if (phaseRef.current === "speaking") {
        stopSpeaking();
        setPhase("listening");
      }
    };
    r.onresult = (e) => {
      if (phaseRef.current === "paused") return;
      let interim = "";
      for (let i = e.resultIndex ?? 0; i < e.results.length; i++) {
        const result = e.results[i];
        const text = result[0].transcript;
        if (result.isFinal && text.trim()) {
          setHeard("");
          setPhase("waiting");
          setTurns((t) => t + 1);
          void sendRef.current(text.trim());
        } else {
          interim += text;
        }
      }
      setHeard(interim);
    };
    r.onerror = (e) => {
      if (e.error === "not-allowed") setError("Microphone access was refused.");
    };
    r.onend = () => {
      if (recognition.current === r) {
        try {
          r.start();
        } catch {
          /* already restarting */
        }
      }
    };
    recognition.current = r;
    try {
      r.start();
    } catch {
      setError("Could not start listening.");
    }
    return () => {
      recognition.current = null;
      r.onend = null;
      r.abort?.();
      stopSpeaking();
    };
  }, []);

  // Speaking: each bot reply that arrives during the call, in order.
  useEffect(() => {
    const fresh = messages.filter((m) => m.seq > spokenUpTo.current && m.role === "bot");
    if (!fresh.length) {
      if (!working && phase === "waiting") setPhase("listening");
      return;
    }
    spokenUpTo.current = fresh[fresh.length - 1].seq;
    setPhase("speaking");
    void (async () => {
      for (const m of fresh) {
        if (phaseRef.current !== "speaking") break;
        await speak(m.content, voice);
      }
      if (phaseRef.current === "speaking") setPhase("listening");
    })();
  }, [messages, working, phase, voice]);

  const hangUp = async () => {
    recognition.current = null;
    stopSpeaking();
    try {
      await recordVoiceCall(bot.id, Math.round((Date.now() - started.current) / 1000), turns);
    } catch {
      /* the call happened; the card is a nicety */
    }
    onEnd();
  };

  const label =
    phase === "listening"
      ? heard || "Listening…"
      : phase === "waiting"
        ? working
          ? `${bot.name} is working…`
          : "Thinking…"
        : phase === "speaking"
          ? `${bot.name} is speaking — talk to interrupt`
          : "Paused";

  return (
    <div className="voicecall" role="dialog" aria-label={`Voice chat with ${bot.name}`}>
      <Avatar
        bot={bot}
        size={56}
        live
        mood={phase === "speaking" ? "typing" : phase === "waiting" ? "thinking" : "idle"}
      />
      <div className="vc-main">
        <div className="vc-title">
          Voice chat with {bot.name}
          <span className={cx("vc-dot", phase)} />
        </div>
        <div className="vc-status">{error ?? label}</div>
        {voices.length > 0 && (
          <select
            className="vc-voice"
            value={voice}
            onChange={(e) => {
              setVoice(e.target.value);
              saveVoice(e.target.value);
            }}
            aria-label="Voice"
          >
            <option value="">Default voice</option>
            {voices.map((v) => (
              <option key={v.name} value={v.name}>
                {v.name} ({v.lang})
              </option>
            ))}
          </select>
        )}
      </div>
      <button
        type="button"
        className="pbtn"
        onClick={() => {
          if (phase === "paused") setPhase("listening");
          else {
            stopSpeaking();
            setPhase("paused");
          }
        }}
      >
        {phase === "paused" ? "Resume" : "Mute"}
      </button>
      <button type="button" className="pbtn danger" onClick={() => void hangUp()}>
        Hang up
      </button>
    </div>
  );
}
