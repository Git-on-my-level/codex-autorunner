// GENERATED FILE - do not edit directly. Source: static_src/
import { clearChatHistory, loadChatHistory, saveChatHistory, } from "./docChatStorage.js?v=510fd0419ed9eddfa5851d4093853609591d2a4765ecd74f3add9600783da27f";
const STORAGE_CONFIG = {
    keyPrefix: "car-ticket-chat-",
    maxMessages: 50,
    version: 1,
};
function normalizeTicketHistoryKey(ticketRef) {
    return String(ticketRef);
}
export function saveTicketChatHistory(ticketRef, messages) {
    saveChatHistory(STORAGE_CONFIG, normalizeTicketHistoryKey(ticketRef), messages);
}
export function loadTicketChatHistory(ticketRef) {
    return loadChatHistory(STORAGE_CONFIG, normalizeTicketHistoryKey(ticketRef));
}
export function clearTicketChatHistory(ticketRef) {
    clearChatHistory(STORAGE_CONFIG, normalizeTicketHistoryKey(ticketRef));
}
