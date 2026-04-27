// GENERATED FILE - do not edit directly. Source: static_src/
import { initDocChatVoice } from "./docChatVoice.js?v=62070ab22f9201700f4cbe1ce8b08b2a7cf7419dd93d9677cdfc7ba5c9537a14";
export async function initTicketVoice() {
    await initDocChatVoice({
        buttonId: "ticket-chat-voice",
        inputId: "ticket-chat-input",
        statusId: "ticket-chat-voice-status",
    });
}
