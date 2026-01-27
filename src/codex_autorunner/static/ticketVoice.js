// GENERATED FILE - do not edit directly. Source: static_src/
import { initDocChatVoice } from "./docChatVoice.js";
export async function initTicketVoice() {
    await initDocChatVoice({
        buttonId: "ticket-chat-voice",
        inputId: "ticket-chat-input",
        statusId: "ticket-chat-voice-status",
    });
}
