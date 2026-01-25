/**
 * Ticket Voice - voice input initialization for ticket chat
 */
import { flash } from "./utils.js";
import { initVoiceInput } from "./voice.js";

const VOICE_TRANSCRIPT_DISCLAIMER_TEXT =
  "Note: the text above was transcribed from voice input and may contain transcription errors.";

function wrapInjectedContext(text: string): string {
  return `<injected context>\n${text}\n</injected context>`;
}

function appendVoiceTranscriptDisclaimer(text: unknown): string {
  const base = text === undefined || text === null ? "" : String(text);
  if (!base.trim()) return base;
  const injection = wrapInjectedContext(VOICE_TRANSCRIPT_DISCLAIMER_TEXT);
  if (base.includes(VOICE_TRANSCRIPT_DISCLAIMER_TEXT) || base.includes(injection)) {
    return base;
  }
  const separator = base.endsWith("\n") ? "\n" : "\n\n";
  return `${base}${separator}${injection}`;
}

function autoResizeTextarea(textarea: HTMLTextAreaElement): void {
  textarea.style.height = "auto";
  textarea.style.height = Math.min(textarea.scrollHeight, 100) + "px";
}

interface TicketVoiceElements {
  voiceBtn: HTMLButtonElement | null;
  input: HTMLTextAreaElement | null;
  voiceStatus: HTMLElement | null;
}

function getTicketVoiceElements(): TicketVoiceElements {
  return {
    voiceBtn: document.getElementById("ticket-chat-voice") as HTMLButtonElement | null,
    input: document.getElementById("ticket-chat-input") as HTMLTextAreaElement | null,
    voiceStatus: document.getElementById("ticket-chat-voice-status") as HTMLElement | null,
  };
}

function applyVoiceTranscript(input: HTMLTextAreaElement, text: string): void {
  if (!text) {
    flash("Voice capture returned no transcript", "error");
    return;
  }
  const current = input.value.trim();
  const prefix = current ? current + " " : "";
  let next = `${prefix}${text}`.trim();
  next = appendVoiceTranscriptDisclaimer(next);
  input.value = next;
  autoResizeTextarea(input);
  input.focus();
  flash("Voice transcript added");
}

export async function initTicketVoice(): Promise<void> {
  const els = getTicketVoiceElements();

  if (!els.voiceBtn || !els.input) {
    return;
  }

  await initVoiceInput({
    button: els.voiceBtn,
    input: els.input,
    statusEl: els.voiceStatus ?? undefined,
    onTranscript: (text) => applyVoiceTranscript(els.input!, text),
    onError: (msg) => {
      if (msg) {
        flash(msg, "error");
        if (els.voiceStatus) {
          els.voiceStatus.textContent = msg;
          els.voiceStatus.classList.remove("hidden");
        }
      }
    },
  }).catch((err) => {
    console.error("Ticket voice init failed", err);
    flash("Voice capture unavailable", "error");
  });
}
