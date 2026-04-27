// GENERATED FILE - do not edit directly. Source: static_src/
import { clearChatHistory, loadChatHistory, saveChatHistory, } from "./docChatStorage.js?v=62070ab22f9201700f4cbe1ce8b08b2a7cf7419dd93d9677cdfc7ba5c9537a14";
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
