/**
 * Ticket Voice - voice input initialization for ticket chat
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
function getTicketVoiceElements() {
    return {
        voiceBtn: document.getElementById("ticket-chat-voice"),
        input: document.getElementById("ticket-chat-input"),
        voiceStatus: document.getElementById("ticket-chat-voice-status"),
    };
}
function applyVoiceTranscript(input, text) {
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
export async function initTicketVoice() {
    const els = getTicketVoiceElements();
    if (!els.voiceBtn || !els.input) {
        return;
    }
    await initVoiceInput({
        button: els.voiceBtn,
        input: els.input,
        statusEl: els.voiceStatus ?? undefined,
        onTranscript: (text) => applyVoiceTranscript(els.input, text),
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
