// GENERATED FILE - do not edit directly. Source: static_src/
/**
 * Generic voice helper for doc/ticket chats.
 */
import { flash } from "./utils.js";
import { initVoiceInput } from "./voice.js";
const VOICE_TRANSCRIPT_DISCLAIMER_TEXT = "Note: the text above was transcribed from voice input and may contain transcription errors.";
function wrapInjectedContext(text) {
    return `<injected context>\n${text}\n</injected context>`;
}
function appendVoiceTranscriptDisclaimer(text) {
    const base = text === undefined || text === null ? "" : String(text);
    if (!base.trim())
        return base;
    const injection = wrapInjectedContext(VOICE_TRANSCRIPT_DISCLAIMER_TEXT);
    if (base.includes(VOICE_TRANSCRIPT_DISCLAIMER_TEXT) || base.includes(injection)) {
        return base;
    }
    const separator = base.endsWith("\n") ? "\n" : "\n\n";
    return `${base}${separator}${injection}`;
}
function autoResizeTextarea(textarea) {
    textarea.style.height = "auto";
    textarea.style.height = Math.min(textarea.scrollHeight, 100) + "px";
}
export async function initDocChatVoice(config) {
    const voiceBtn = document.getElementById(config.buttonId);
    const input = document.getElementById(config.inputId);
    const statusEl = config.statusId
        ? document.getElementById(config.statusId)
        : null;
    if (!voiceBtn || !input)
        return;
    await initVoiceInput({
        button: voiceBtn,
        input,
        statusEl: statusEl ?? undefined,
        onTranscript: (text) => {
            if (!text) {
                flash("Voice capture returned no transcript", "error");
                return;
            }
            const current = input.value.trim();
            const prefix = current ? `${current} ` : "";
            let next = `${prefix}${text}`.trim();
            next = appendVoiceTranscriptDisclaimer(next);
            input.value = next;
            autoResizeTextarea(input);
            input.focus();
            flash("Voice transcript added");
        },
        onError: (msg) => {
            if (msg) {
                flash(msg, "error");
                if (statusEl) {
                    statusEl.textContent = msg;
                    statusEl.classList.remove("hidden");
                }
            }
        },
    }).catch((err) => {
        console.error("Voice init failed", err);
        flash("Voice capture unavailable", "error");
    });
}
