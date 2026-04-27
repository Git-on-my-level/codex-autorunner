// GENERATED FILE - do not edit directly. Source: static_src/
import { flash } from "./utils.js?v=62070ab22f9201700f4cbe1ce8b08b2a7cf7419dd93d9677cdfc7ba5c9537a14";
import { initVoiceInput } from "./voice.js?v=62070ab22f9201700f4cbe1ce8b08b2a7cf7419dd93d9677cdfc7ba5c9537a14";
import { safeFocus, persistTextInputDraft, setTextInputEnabled, } from "./terminalTextInput.js?v=62070ab22f9201700f4cbe1ce8b08b2a7cf7419dd93d9677cdfc7ba5c9537a14";
const textEncoder = new TextEncoder();
const VOICE_TRANSCRIPT_DISCLAIMER_TEXT = (typeof CONSTANTS !== "undefined" && CONSTANTS.PROMPTS?.VOICE_TRANSCRIPT_DISCLAIMER) ||
    "Note: transcribed from user voice. If confusing or possibly inaccurate and you cannot infer the intention please clarify before proceeding.";
const INJECTED_CONTEXT_TAG_RE = /<injected context>/i;
function wrapInjectedContext(text) {
    return `<injected context>\n${text}\n</injected context>`;
}
function wrapInjectedContextIfNeeded(text) {
    if (!text)
        return text;
    return INJECTED_CONTEXT_TAG_RE.test(text) ? text : wrapInjectedContext(text);
}
import { CONSTANTS } from "./constants.js?v=62070ab22f9201700f4cbe1ce8b08b2a7cf7419dd93d9677cdfc7ba5c9537a14";
export function createVoiceState() {
    return {
        textVoiceBtn: null,
        voiceBtn: null,
        voiceStatus: null,
        voiceController: null,
        voiceKeyActive: false,
        mobileVoiceBtn: null,
        mobileVoiceController: null,
        textVoiceController: null,
    };
}
export function appendVoiceTranscriptDisclaimer(text) {
    const base = text === undefined || text === null ? "" : String(text);
    if (!base.trim())
        return base;
    const injection = wrapInjectedContextIfNeeded(VOICE_TRANSCRIPT_DISCLAIMER_TEXT);
    if (base.includes(VOICE_TRANSCRIPT_DISCLAIMER_TEXT) ||
        (typeof injection === "string" && base.includes(injection || ""))) {
        return base;
    }
    const separator = base.endsWith("\n") ? "\n" : "\n\n";
    const injectionValue = injection || "";
    return `${base}${separator}${injectionValue}`;
}
export function insertTranscriptIntoTextInput(_voiceState, text, textInputState, isTouchDevice, textInputDeps) {
    if (!text)
        return false;
    if (!textInputState.textInputTextareaEl)
        return false;
    if (!textInputState.textInputEnabled) {
        setTextInputEnabled(textInputState, true, isTouchDevice, textInputDeps, { focus: true, focusTextarea: true });
    }
    const transcript = String(text).trim();
    if (!transcript)
        return false;
    const existing = textInputState.textInputTextareaEl.value || "";
    let next = existing;
    if (existing && !/\s$/.test(existing)) {
        next += " ";
    }
    next += transcript;
    next = appendVoiceTranscriptDisclaimer(next);
    textInputState.textInputTextareaEl.value = next;
    persistTextInputDraft(textInputState);
    textInputDeps.updateComposerSticky();
    safeFocus(textInputState.textInputTextareaEl);
    return true;
}
export function sendVoiceTranscript(voiceState, text, socket, deps) {
    if (!text) {
        flash("Voice capture returned no transcript", "error");
        return;
    }
    const textInputState = deps.getTextInputState();
    if (deps.isTouchDevice() || textInputState.textInputEnabled) {
        if (insertTranscriptIntoTextInput(voiceState, text, textInputState, deps.isTouchDevice(), deps.getTextInputDeps())) {
            flash("Voice transcript added to text input");
            return;
        }
    }
    if (!socket || socket.readyState !== WebSocket.OPEN) {
        flash("Connect the terminal before using voice input", "error");
        if (voiceState.voiceStatus) {
            voiceState.voiceStatus.textContent = "Connect to send voice";
            voiceState.voiceStatus.classList.remove("hidden");
        }
        return;
    }
    const message = appendVoiceTranscriptDisclaimer(text);
    const payload = message.endsWith("\n") ? message : `${message}\n`;
    socket.send(textEncoder.encode(payload));
    deps.getTerm()?.focus?.();
    flash("Voice transcript sent to terminal");
}
export function matchesVoiceHotkey(event) {
    return event.key && event.key.toLowerCase() === "v" && event.altKey;
}
export function handleVoiceHotkeyDown(voiceState, event, socket) {
    if (!voiceState.voiceController || voiceState.voiceKeyActive)
        return;
    if (!matchesVoiceHotkey(event))
        return;
    if (!socket || socket.readyState !== WebSocket.OPEN) {
        flash("Connect the terminal before using voice input", "error");
        return;
    }
    event.preventDefault();
    event.stopPropagation();
    voiceState.voiceKeyActive = true;
    voiceState.voiceController.start();
}
export function handleVoiceHotkeyUp(voiceState, event) {
    if (!voiceState.voiceKeyActive)
        return;
    if (event && matchesVoiceHotkey(event)) {
        event.preventDefault();
        event.stopPropagation();
    }
    voiceState.voiceKeyActive = false;
    voiceState.voiceController?.stop();
}
export function initTerminalVoice(voiceState, socket, deps) {
    voiceState.voiceBtn = document.getElementById("terminal-voice");
    voiceState.voiceStatus = document.getElementById("terminal-voice-status");
    voiceState.mobileVoiceBtn = document.getElementById("terminal-mobile-voice");
    voiceState.textVoiceBtn = document.getElementById("terminal-text-voice");
    if (voiceState.voiceBtn && voiceState.voiceStatus) {
        initVoiceInput({
            button: voiceState.voiceBtn,
            input: null,
            statusEl: voiceState.voiceStatus,
            onTranscript: (text) => sendVoiceTranscript(voiceState, text, socket, deps),
            onError: (msg) => {
                if (!msg)
                    return;
                flash(msg, "error");
                if (voiceState.voiceStatus) {
                    voiceState.voiceStatus.textContent = msg;
                    voiceState.voiceStatus.classList.remove("hidden");
                }
            },
        })
            .then((controller) => {
            if (!controller) {
                voiceState.voiceBtn?.closest(".terminal-voice")?.classList.add("hidden");
                return;
            }
            voiceState.voiceController = controller;
            if (voiceState.voiceStatus) {
                const base = voiceState.voiceStatus.textContent || "Hold to talk";
                voiceState.voiceStatus.textContent = `${base} (Alt+V)`;
                voiceState.voiceStatus.classList.remove("hidden");
            }
            const boundKeyDown = (e) => handleVoiceHotkeyDown(voiceState, e, deps.getSocket());
            const boundKeyUp = (e) => handleVoiceHotkeyUp(voiceState, e);
            window.addEventListener("keydown", boundKeyDown);
            window.addEventListener("keyup", boundKeyUp);
            window.addEventListener("blur", () => {
                if (voiceState.voiceKeyActive) {
                    voiceState.voiceKeyActive = false;
                    voiceState.voiceController?.stop();
                }
            });
        })
            .catch((err) => {
            console.error("Voice init failed", err);
            flash("Voice capture unavailable", "error");
            if (voiceState.voiceStatus) {
                voiceState.voiceStatus.textContent = "Voice unavailable";
                voiceState.voiceStatus.classList.remove("hidden");
            }
        });
    }
    if (voiceState.mobileVoiceBtn) {
        initVoiceInput({
            button: voiceState.mobileVoiceBtn,
            input: null,
            statusEl: null,
            onTranscript: (text) => sendVoiceTranscript(voiceState, text, deps.getSocket(), deps),
            onError: (msg) => {
                if (!msg)
                    return;
                flash(msg, "error");
            },
        })
            .then((controller) => {
            if (!controller) {
                voiceState.mobileVoiceBtn.classList.add("hidden");
                return;
            }
            voiceState.mobileVoiceController = controller;
        })
            .catch((err) => {
            console.error("Mobile voice init failed", err);
            voiceState.mobileVoiceBtn.classList.add("hidden");
        });
    }
    if (voiceState.textVoiceBtn) {
        initVoiceInput({
            button: voiceState.textVoiceBtn,
            input: null,
            statusEl: null,
            onTranscript: (text) => sendVoiceTranscript(voiceState, text, deps.getSocket(), deps),
            onError: (msg) => {
                if (!msg)
                    return;
                flash(msg, "error");
            },
        })
            .then((controller) => {
            if (!controller) {
                voiceState.textVoiceBtn.classList.add("hidden");
                return;
            }
            voiceState.textVoiceController = controller;
        })
            .catch((err) => {
            console.error("Text voice init failed", err);
            voiceState.textVoiceBtn.classList.add("hidden");
        });
    }
}
