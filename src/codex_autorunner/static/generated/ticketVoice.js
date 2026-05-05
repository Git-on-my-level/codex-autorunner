// GENERATED FILE - do not edit directly. Source: static_src/
import { initDocChatVoice } from "./docChatVoice.js?v=be399e9b80baaceac895399d521c7e33ba6116a6282d86fe16aaac8dd380e544";
export async function initTicketVoice() {
    await initDocChatVoice({
        buttonId: "ticket-chat-voice",
        inputId: "ticket-chat-input",
        statusId: "ticket-chat-voice-status",
    });
}
