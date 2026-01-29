// GENERATED FILE - do not edit directly. Source: static_src/
import { clearChatHistory, loadChatHistory, saveChatHistory, } from "./docChatStorage.js";
const STORAGE_CONFIG = {
    keyPrefix: "car-ticket-chat-",
    maxMessages: 50,
    version: 1,
};
export function saveTicketChatHistory(ticketIndex, messages) {
    saveChatHistory(STORAGE_CONFIG, String(ticketIndex), messages);
}
export function loadTicketChatHistory(ticketIndex) {
    return loadChatHistory(STORAGE_CONFIG, String(ticketIndex));
}
export function clearTicketChatHistory(ticketIndex) {
    clearChatHistory(STORAGE_CONFIG, String(ticketIndex));
}
