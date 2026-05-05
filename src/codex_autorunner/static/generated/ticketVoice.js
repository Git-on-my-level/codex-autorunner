// GENERATED FILE - do not edit directly. Source: static_src/
import { initDocChatVoice } from "./docChatVoice.js?v=7fa8004f6840e214503b15a447aff6b141a7ad76cba89a9cf20138dbd2d88456";
export async function initTicketVoice() {
    await initDocChatVoice({
        buttonId: "ticket-chat-voice",
        inputId: "ticket-chat-input",
        statusId: "ticket-chat-voice-status",
    });
}
