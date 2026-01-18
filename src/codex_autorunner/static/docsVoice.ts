import { flash } from "./utils.js";
import { initVoiceInput } from "./voice.js";
import { chatUI } from "./docsElements.js";
import { VOICE_TRANSCRIPT_DISCLAIMER_TEXT } from "./docsState.js";
import { autoResizeTextarea } from "./docsUi.js";

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

function applyVoiceTranscript(text: string): void {
  if (!text) {
    flash("Voice capture returned no transcript", "error");
    return;
  }
  const current = chatUI.input.value.trim();
  const prefix = current ? current + " " : "";
  let next = `${prefix}${text}`.trim();
  next = appendVoiceTranscriptDisclaimer(next);
  chatUI.input.value = next;
  autoResizeTextarea(chatUI.input);
  chatUI.input.focus();
  flash("Voice transcript added");
}

export async function initDocVoice(): Promise<void> {
  if (!chatUI.voiceBtn || !chatUI.input) {
    return;
  }
  await initVoiceInput({
    button: chatUI.voiceBtn,
    input: chatUI.input,
    statusEl: chatUI.voiceStatus,
    onTranscript: applyVoiceTranscript,
    onError: (msg) => {
      if (msg) {
        flash(msg, "error");
        if (chatUI.voiceStatus) {
          chatUI.voiceStatus.textContent = msg;
          chatUI.voiceStatus.classList.remove("hidden");
        }
      }
    },
  }).catch((err) => {
    console.error("Voice init failed", err);
    flash("Voice capture unavailable", "error");
  });
}
