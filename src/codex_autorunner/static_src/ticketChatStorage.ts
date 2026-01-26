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

export function saveTicketChatHistory(ticketIndex: number, messages: ChatMessage[]): void {
  saveChatHistory(STORAGE_CONFIG, String(ticketIndex), messages);
}

export function loadTicketChatHistory(ticketIndex: number): ChatMessage[] {
  return loadChatHistory(STORAGE_CONFIG, String(ticketIndex));
}

export function clearTicketChatHistory(ticketIndex: number): void {
  clearChatHistory(STORAGE_CONFIG, String(ticketIndex));
}
