import type { ChatMessage } from "./docChatCore.js";
import {
  clearChatHistory,
  loadChatHistory,
  saveChatHistory,
  type ChatStorageConfig,
} from "./docChatStorage.js";

const STORAGE_CONFIG: ChatStorageConfig = {
  keyPrefix: "car-ticket-chat-",
  maxMessages: 50,
  version: 1,
};

function normalizeTicketHistoryKey(ticketRef: number | string): string {
  return String(ticketRef);
}

export function saveTicketChatHistory(
  ticketRef: number | string,
  messages: ChatMessage[]
): void {
  saveChatHistory(STORAGE_CONFIG, normalizeTicketHistoryKey(ticketRef), messages);
}

export function loadTicketChatHistory(ticketRef: number | string): ChatMessage[] {
  return loadChatHistory(STORAGE_CONFIG, normalizeTicketHistoryKey(ticketRef));
}

export function clearTicketChatHistory(ticketRef: number | string): void {
  clearChatHistory(STORAGE_CONFIG, normalizeTicketHistoryKey(ticketRef));
}
