// GENERATED FILE - do not edit directly. Source: static_src/
import { clearChatHistory, loadChatHistory, saveChatHistory, } from "./docChatStorage.js?v=7fa8004f6840e214503b15a447aff6b141a7ad76cba89a9cf20138dbd2d88456";
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
