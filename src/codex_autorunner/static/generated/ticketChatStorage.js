// GENERATED FILE - do not edit directly. Source: static_src/
import { clearChatHistory, loadChatHistory, saveChatHistory, } from "./docChatStorage.js?v=ac0c75a9b48302989280b9278c713a406824bfb9e317de690d6949a4bd54d2e3";
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
