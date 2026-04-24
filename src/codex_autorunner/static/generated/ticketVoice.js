// GENERATED FILE - do not edit directly. Source: static_src/
import { initDocChatVoice } from "./docChatVoice.js?v=ac0c75a9b48302989280b9278c713a406824bfb9e317de690d6949a4bd54d2e3";
export async function initTicketVoice() {
    await initDocChatVoice({
        buttonId: "ticket-chat-voice",
        inputId: "ticket-chat-input",
        statusId: "ticket-chat-voice-status",
    });
}
