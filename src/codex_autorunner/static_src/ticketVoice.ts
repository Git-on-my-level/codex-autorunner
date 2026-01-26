import { initDocChatVoice } from "./docChatVoice.js";

export async function initTicketVoice(): Promise<void> {
  await initDocChatVoice({
    buttonId: "ticket-chat-voice",
    inputId: "ticket-chat-input",
    statusId: "ticket-chat-voice-status",
  });
}
